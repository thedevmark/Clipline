"""One companion source-action owner. SQLite connections stay operation-local."""
from dataclasses import dataclass, replace
import threading
import uuid

from native.services.workspace_library import WorkspaceLibrary
from native.services.durable_twitch_intake import DurableTwitchIntake
from native.services.durable_native_exports import ExportOwnershipError
from native.services.twitch_publication import PublicationGuardError


@dataclass(frozen=True)
class SourceView:
    state: str = 'idle'
    message: str = 'Select a clip to download its source.'
    clip_id: str | None = None
    job_id: str | None = None
    busy: bool = False
    can_download: bool = True
    can_authorize: bool = True
    can_resume: bool = False
    can_cancel: bool = False
    can_cleanup: bool = False
    app_close_allowed: bool = True


class WorkspaceIntakeController:
    """Keep this instance until its actual capabilities are known closed.

    Views contain no connection/token/native owner. Generic worker callbacks
    receive these views only; actual capabilities stay strongly held here.
    """
    def __init__(self,workspace,runtime):
        if type(workspace) is not WorkspaceLibrary:
            raise ValueError('Use the canonical WorkspaceLibrary owner.')
        self.workspace,self.runtime=workspace,runtime
        self._operation=threading.Lock(); self._state=threading.RLock()
        self._busy=False; self._view=SourceView(); self._job=None; self._clip=None
        self._executions=[]; self._publications=[]
        if runtime is None: self._publish('unsupported','Source downloads are not configured.')

    def status(self):
        with self._state: return self._view

    def _active(self):
        return any(not owner.closed for owner in (*self._executions,*self._publications))

    def _unfinished(self):
        return self._job is not None and self._job['state'] in ('queued','running','cancel-requested','interrupted')

    def _publish(self,state,message):
        with self._state:
            pending=self._job is not None and self._job['state']=='queued'
            active=self._active()
            ready=not self._busy and not self._unfinished() and not active and self.runtime is not None
            self._view=SourceView(state,message,self._clip,self._job['id'] if self._job else None,self._busy,
                ready,ready,pending and not self._busy and not active,
                pending and not self._busy and not active,active and not self._busy,
                not self._busy and not pending and not active)
            return self._view

    def _retain(self,error):
        with self._state:
            owners=error.executions if isinstance(error,ExportOwnershipError) else ()
            for owner in owners:
                if all(owner is not old for old in self._executions): self._executions.append(owner)
                binding=getattr(owner,'binding',{})
                if self._job is None and isinstance(binding,dict) and binding.get('job_id'):
                    self._job={'id':binding['job_id'],'state':'interrupted'}
            if isinstance(error,PublicationGuardError):
                owner=error.publication
                if all(owner is not old for old in self._publications): self._publications.append(owner)

    def _run(self,operation):
        if threading.current_thread() is threading.main_thread():
            return replace(self.status(),state='blocked',message='Source operations require a background owner.')
        if not self._operation.acquire(False):
            return replace(self.status(),state='busy',message='A source operation is already running.',app_close_allowed=False)
        self._busy=True; self._publish(self._view.state,self._view.message)
        try:
            operation()
        except (ExportOwnershipError,PublicationGuardError) as error:
            self._retain(error)
            self._publish('pending','Source work needs attention. Retained resources must be stopped before closing Clipline.')
        except BaseException:
            # No exception carrying a live capability reaches WorkerJob's string signal.
            recovered=False
            if not self._active() and self._job is not None:
                try:
                    with self.workspace._library() as library:
                        self._job=library.jobs.job(self._job['id'])
                        if self._job['state']=='completed':
                            bound=library.twitch_source(self._clip)
                            library.verify_asset(bound['asset_id'])
                            self._publish('completed','Source saved. Its registered file was verified.')
                            recovered=True
                except Exception: pass
            if not recovered:
                self._publish('failed','The source action did not finish. Check the saved source records before trying again.')
        finally:
            self._busy=False
            self._publish(self._view.state,self._view.message)
            self._operation.release()
        return self.status()

    def _owner(self,library):
        return DurableTwitchIntake(library,self.workspace.directory/'source-intake',self.runtime.client())

    def _accept(self,result,library):
        if result is None:
            self._job=library.jobs.job(self._job['id'])
            self._publish('pending' if self._job['state']=='queued' else 'blocked',
                'The selected source is queued. Resume when a source slot is available, or cancel this task.'
                if self._job['state']=='queued' else 'The selected source task is not eligible to start. It was not retried.')
            return
        if result.execution is not None:
            with self._state:
                if all(result.execution is not old for old in self._executions): self._executions.append(result.execution)
        if result.job is not None: self._job=result.job
        if result.reused:
            self._job=None
            self._publish('reused','Saved source verified. No download was needed.')
        elif self._active():
            self._publish('pending','Source work is interrupted. Stop the retained resources before another action.')
        elif result.job['state']=='completed':
            self._publish('completed','Source downloaded. It is available in Shared files.')
        elif result.job['state']=='cancelled':
            self._publish('cancelled','Source download cancelled. No new source was registered.')
        elif result.error_code=='AUTH_REQUIRED':
            self._publish('authorization_required','Allow clip downloads for this channel, then choose Download source again.')
        elif result.error_code in ('UNSUPPORTED_SOURCE','DURATION_LIMIT'):
            self._publish('unsupported','This source is outside the supported local intake profile.')
        elif result.job['state']=='interrupted':
            self._publish('interrupted','The source task is interrupted. It was not retried.')
        else:
            self._publish('failed','The source could not be downloaded. No new source was registered.')

    def download(self,clip_id,*,cancel):
        def operation():
            if self._active() or self._unfinished():
                self._publish('pending','Finish or cancel the pending source task first.'); return
            if cancel.is_set():
                self._publish('cancelled','Source download cancelled before submission.'); return
            if self.runtime is None:
                self._publish('unsupported','Source downloads are not configured.'); return
            self._clip=clip_id; self._job=None
            with self.workspace._library() as library:
                selected=library.twitch_clip(clip_id)
                if selected.duration_us>180_000_000:
                    self._publish('unsupported','This clip exceeds the local three-minute source limit.'); return
                owner=self._owner(library)
                submitted=owner.submit(clip_id,idempotency_key=str(uuid.uuid4()))
                if submitted.reused: self._accept(submitted,library); return
                self._job=submitted.job
                self._accept(owner.run_next(cancel=cancel,job_id=submitted.job['id']),library)
        return self._run(operation)

    def resume(self,*,cancel):
        def operation():
            if self._active() or self._job is None:
                self._publish('pending','The pending source task needs attention.'); return
            with self.workspace._library() as library:
                current=library.jobs.job(self._job['id']); self._job=current
                if current['state']!='queued':
                    self._publish('interrupted','This source task is no longer queued. It was not retried.'); return
                self._accept(self._owner(library).run_next(cancel=cancel,job_id=current['id']),library)
        return self._run(operation)

    def cancel_pending(self):
        def operation():
            if self._active() or self._job is None: return
            with self.workspace._library() as library:
                current=library.jobs.job(self._job['id'])
                if current['state']=='queued':
                    current=library.jobs.request_cancel(current['id'],expected_revision=current['revision'])
                self._job=current
                self._publish('cancelled' if current['state']=='cancelled' else 'interrupted',
                    'Queued source task cancelled.' if current['state']=='cancelled' else 'This source task is not queued. It was not retried.')
        return self._run(operation)

    def cleanup(self):
        def operation():
            with self.workspace._library() as library:
                owner=self._owner(library)
                for execution in self._executions:
                    if not execution.closed:
                        if execution.binding.get('job_id'):
                            owner.cleanup_pending(execution.binding['job_id'],execution=execution)
                        else:
                            # Reuse verification has no claim; retain and close its actual guard owner.
                            execution.stop()
                        if not execution.closed: self._publish('pending','Source cleanup remains unconfirmed.'); return
                for publication in self._publications:
                    if not publication.closed: publication.close()
                if self._job and self._job.get('id'):
                    self._job=library.jobs.job(self._job['id'])
                if self._job and self._job['state']=='completed':
                    bound=library.twitch_source(self._clip)
                    library.verify_asset(bound['asset_id'])
                    self._publish('completed','Source saved. Retained source handles are closed.')
                else:
                    self._publish('interrupted','Retained source resources stopped. The task was not retried or reconciled.')
        return self._run(operation)

    def authorize(self,clip_id,job,*,cancel):
        def operation():
            if self._active() or self._unfinished():
                self._publish('pending','Finish or cancel the pending source task first.'); return
            if self.runtime is None:
                self._publish('unsupported','Source downloads are not configured.'); return
            self._clip=clip_id
            with self.workspace._library() as library: selected=library.twitch_clip(clip_id)
            from native.services import twitch_auth
            try:
                summary=twitch_auth.authorize_clip_downloads(job,cancel,
                    expected_broadcaster_id=selected.broadcaster_id,bound_client_id=self.runtime.client_id)
            except Exception:
                self._publish('cancelled' if cancel.is_set() else 'authorization_required',
                    'Clip authorization cancelled. Previous authorization remains unchanged.' if cancel.is_set()
                    else 'Clip authorization did not finish. Previous authorization remains unchanged.')
                return
            self._publish('authorized',f'Clip downloads allowed for {summary["login"]}. Choose Download source when ready.')
        return self._run(operation)
