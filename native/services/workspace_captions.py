"""Active-Moment caption adjunct. WorkspaceEditorController remains the session owner."""
from dataclasses import dataclass, replace
import json
from pathlib import Path
import threading
from copy import deepcopy
import uuid

from native.core.creator_library import CreatorLibrary
from native.services.caption_audio import CaptionAudioPreparer
from native.services.caption_worker.client import CaptionClient
from native.services.durable_captions import DurableCaptionRunner
from native.services.moment_captions import MomentCaptionSelector
from native.services.caption_timing import TimingRepairController, TimingInputError
from native.services.native_captions import read_native_pin
from native.core.moment_captions import _evidence
import hashlib, os


def require(value, message):
    if not value:
        raise ValueError(message)


def copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


@dataclass(frozen=True)
class CaptionOutcome:
    state: str
    message: str
    context_token: str | None = None
    title: str = ''
    source_range: tuple[int,int] | None = None
    analysis_range: tuple[int,int] | None = None
    profile_name: str = ''
    streams: tuple = ()
    track_choices: tuple = ()
    tracks: tuple = ()
    proposal: object | None = None
    repair: object | None = None
    requires_reconciliation: bool = False
    can_generate: bool = False
    can_accept: bool = False
    can_cleanup: bool = False


class WorkspaceCaptions:
    def __init__(self, editor, runtime):
        self.editor, self.runtime = editor, runtime
        self.directory = editor.workspace.directory / 'captions'
        self.preparer = CaptionAudioPreparer(runtime.ffmpeg[0],runtime.ffprobe[0],python=runtime.python[0])
        self.client = CaptionClient(runtime.python[0],runtime.worker[0])
        self.runner = None
        self._context = self._session = self._binding = self._adapter = None
        self._tracks = None
        self._token = None
        self._native_uncertain = False
        self._selection_revision = 0
        self._retained = None
        self._view = CaptionOutcome('unavailable','Open a Moment edit to use captions.')

    @property
    def unsettled(self):
        return bool(self._native_uncertain or self._retained is not None
                    or self.preparer.has_unsettled or self.client.has_unsettled
                    or self.runner is not None and (self.runner.has_unsettled or self.runner.interrupted_job is not None))

    def refresh_barrier(self):
        e=self.editor
        if e._session is None:return
        _,links=e._inspect()
        for row in links:
            identity=os.path.normcase(str(e._view.working.path.resolve(strict=True)))
            binding=hashlib.sha256(f"{identity}:{row['owner']}".encode()).hexdigest()
            if (self.directory/'repair'/binding/'unsettled-application.json').exists():
                self._native_uncertain=True

    def status(self):
        return deepcopy(self._view)

    def _outcome(self, state, message, **values):
        values = {**{'requires_reconciliation':self.unsettled,'can_generate':False,'can_accept':False,
                     'can_cleanup':self.unsettled and not self._native_uncertain},**values}
        self._view = replace(self._view,state=state,message=message,**values)
        return self._view

    def dispatch(self, fn, *, observation=False):
        result = []
        def operation():
            try:
                require(observation or not self.unsettled,'Check or clean up the retained caption operation first')
                result.append(fn())
            except BaseException as error:
                # Capture actual capabilities before JobRunner can stringify an exception.
                actual = getattr(error,'owner',None)
                if actual is not None and actual is not self.editor._session:
                    self._retained = actual
                if self._adapter is not None and self._adapter.barrier.exists():
                    self._native_uncertain = True
                result.append(self._outcome('blocked',str(error)))
        editor_result = self.editor._run(operation)
        if not result:
            return replace(self._view,state='blocked',message=editor_result.message,can_generate=False,can_accept=False)
        return deepcopy(result[0])

    def _admit(self, token=None, *, observation=False):
        e = self.editor
        require(e._session is not None and not e._quit_attempted and (observation or e._blocked_reason is None),
                'A verified open editor is required; reconcile its separate document uncertainty first')
        require(e._view.state == 'open' or observation and e._view.state=='reconciliation_required','Finish the existing editor operation first')
        e._assert_facts(verify=True)
        e._assert_publication_base()
        e._inspect()
        if token is not None:
            require(token == self._token and e._session is self._session,'Reload captions for this exact editor session')
            require(self._context == self._identity(),'The active Moment, Kit, source or working path changed')
        return e

    def _identity(self):
        e=self.editor;f=e._facts
        return (f.moment.id,f.moment.revision,f.kit.descriptor_sha256,f.source.id,f.source.sha256,
                str(e._view.working.path.resolve(strict=True)))

    def _discover(self):
        e=self.editor;bridge=e._session.bridge
        before=bridge.execute('project.inspect',{})
        validation=bridge.execute('clipline.validate',{'expectedRevision':before['revision']})
        owners=validation['owners']
        require(len(owners)==1,'Select a Moment edit with one linked Creator Kit owner')
        row=owners[0];owner=row['owner']
        require(all(any(r.get('role')=='gameplay' and r.get('audio')=='inherit' for r in unit['roles']) for unit in row['units']),
                'This caption workflow requires enabled inherited gameplay audio from the source')
        choices=[]
        for role in ('shared','landscape','portrait'):
            seq=row['sequences'][role]
            require(type(seq) is int and seq>0,'Native linked sequence identity is invalid')
            info=bridge.execute('sequence.inspect',{'item':seq})
            require(type(info.get('id')) is int and info['id']==seq,'Native track inventory belongs to another sequence')
            audio=info.get('audio')
            require(isinstance(audio,list) and len(audio)<=128,'A bounded native audio inventory is required')
            linked=[identifier for unit in row['units'] for source_role in unit['roles']
                    if source_role['role']=='gameplay' for identifier in source_role['nativeAudioClips'][role]]
            require(linked and all(type(identifier) is int for identifier in linked),'Native gameplay audio must be linked in every format')
            any_solo=any(t.get('solo') is True for t in audio)
            for identifier in linked:
                matches=[(t,item) for t in audio for item in t.get('items',[]) if item.get('clip')==identifier]
                require(len(matches)==1 and matches[0][0].get('enabled') is True
                    and matches[0][0].get('muted') is False and matches[0][1].get('enabled') is True
                    and (not any_solo or matches[0][0].get('solo') is True),
                    f'Enable inherited gameplay audio in the {role} sequence before generating captions.')
            tracks=info.get('captionTracks')
            require(isinstance(tracks,list) and len(tracks)<=128,'This editor needs the qualified caption inventory extension')
            eligible=[copy(t) for t in tracks if t.get('enabled') is True and t.get('locked') is False
                      and type(t.get('id')) is int and t['id']>0 and isinstance(t.get('style'),dict)]
            require(eligible,f'Open this Creator Kit in Filmcraft and save an enabled, unlocked caption track in its {role} sequence.')
            choices.append({'role':role,'sequence':seq,'tracks':eligible})
        after=bridge.execute('project.inspect',{})
        require(after==before,'The editor changed during caption track discovery')
        return owner,choices

    def _repair_adapter(self):
        e=self.editor
        binding=(id(e._session),e._view.working,self._owner,json.dumps(self._tracks,sort_keys=True))
        if self._binding != binding:
            self._adapter=TimingRepairController(e.workspace.database,e._session.bridge,e._view.working,
                owner=self._owner,tracks=self._tracks,engine_version=e.editor.version,
                receipts_directory=self.directory/'repair')
            self._binding=binding
        if self._adapter.barrier.exists():self._native_uncertain=True
        return self._adapter

    def context(self,moment_id,revision,*,cancel=None):
        def operation():
            e=self._admit();f=e._facts
            require((moment_id,revision)==(f.moment.id,f.moment.revision),'Select the active Moment revision')
            self.runtime.verify()
            owner,choices=self._discover()
            native_pin=read_native_pin(e._session.bridge,owner)
            metadata=e._session.bridge.execute('metadata.get',{'item':owner})['metadata'].get('clipline.acceptedCaptions.v1')
            self._owner=owner;self._session=e._session;self._context=self._identity();self._token=str(uuid.uuid4())
            self._binding=None;self._tracks=None
            if native_pin is not None:
                body=json.loads(metadata)
                require(body['owner']==owner and body['pin']==native_pin,'Native accepted caption identity differs')
                self._tracks=copy(body['tracks'])
                require(len(self._tracks)==3,'Native accepted track binding is incomplete')
                for group in choices:
                    matches=[t for t in self._tracks if t['sequence']==group['sequence']]
                    require(len(matches)==1 and any(t['id']==matches[0]['track'] for t in group['tracks']),
                            'Restore the exact accepted caption track before continuing')
            else:
                self._tracks=[self._track(g,g['tracks'][0]['id']) for g in choices] if all(len(g['tracks'])==1 for g in choices) else None
            self._view=CaptionOutcome('ready','Choose the audio and caption tracks, then generate a proposal.',
                context_token=self._token,title=f.moment.title,source_range=(f.moment.source_in_us,f.moment.source_out_us),
                profile_name=self.runtime.display_name,track_choices=tuple(copy(choices)),tracks=tuple(copy(self._tracks or [])))
            # Reopen follows the exact native accepted pin; never infer a newer canonical head.
            if native_pin is not None:
                repair=self._repair_adapter().load(native_pin['transcript_id'],native_pin['revision'])
                self._remember_repair(repair)
            with CreatorLibrary(e.workspace.database) as library:
                saved=library.caption_selections.selected(moment_id,revision)
                self._selection_revision=saved['selection_revision'] if saved else 0
                if saved is not None:
                    proposal=self._proposal(library,saved)
                    self._view=replace(self._view,proposal=proposal,can_accept=library.captions.head(saved['transcript_id'])==0)
                    if library.captions.head(saved['transcript_id'])>0 and self._tracks is not None:
                        # A saved explicit proposal may already have human history. Opening
                        # it is an explicit review action; no newer machine run is inferred.
                        if native_pin is None:self._remember_repair(self._repair_adapter().load(saved['transcript_id'],library.captions.head(saved['transcript_id'])))
            if self._native_uncertain:
                adapter=self._repair_adapter();attempt=adapter._unsettled()
                return self._remember_repair(adapter.load(attempt['transcript_id'],attempt['revision']))
            inspection=self.preparer.inspect_source(f.source.path,f.source.sha256,self.directory/'inspection',cancel=cancel)
            streams=inspection['streams']
            require(len(streams)==1,'This caption workflow needs one source audio stream. Select a compatible saved source.')
            stream=streams[0]
            # Integral microseconds on a 16kHz grid are multiples of 125us.
            start,end=self.preparer.cover_range(f.moment.source_in_us,f.moment.source_out_us,stream['duration_us'])
            return self._outcome(self._view.state if self._view.repair is not None else 'ready','Review the source audio and tracks. Generation does not accept words.',
                streams=tuple(copy(streams)),analysis_range=(start,end),can_generate=not self._native_uncertain,
                can_accept=self._view.proposal is not None and self._view.can_accept)
        return self.dispatch(operation,observation=self._native_uncertain)

    def _track(self,group,track_id):
        return {'sequence':group['sequence'],'track':track_id,'rules':copy(self.runtime.projection['rules']),
                'defaultSettings':self.runtime.projection['defaultSettings']}

    def _select_tracks(self,selection):
        groups=self._view.track_choices
        require(isinstance(selection,list) and len(selection)==3,'Select one caption track for each native format')
        result=[]
        for group in groups:
            values=[x for x in selection if x.get('sequence')==group['sequence']]
            require(len(values)==1 and set(values[0])=={'sequence','track'} and
                type(values[0]['track']) is int and any(t['id']==values[0]['track'] for t in group['tracks']),
                'Select an existing eligible native caption track')
            result.append(self._track(group,values[0]['track']))
        if self._tracks is not None and self._view.repair is not None:
            require([(t['sequence'],t['track']) for t in result]==[(t['sequence'],t['track']) for t in self._tracks],'Keep the exact applied native caption track bindings')
            result=copy(self._tracks)
        self._tracks=result;self._binding=None

    @staticmethod
    def _proposal(library,selected):
        tid=selected['transcript_id'];h=library.captions.proposal(tid,selected['hypothesis_id'],'hypothesis')
        a=library.captions.proposal(tid,selected['alignment_id'],'alignment') if selected['alignment_id'] else None
        return {**copy(selected),'text':' '.join(w['text'] for w in h['words']),
            'word_count':len(h['words']),'unresolved':sum(w['timing']['status'] not in ('aligned','manually_timed','inherited') for w in a['words']) if a else len(h['words']),
            'evidence':{k:copy(h[k]) for k in ('metrics','warnings') if k in h}}

    def generate(self,token,selection,*,cancel=None):
        def operation():
            e=self._admit(token);require(set(selection)=={'stream_index','channels','tracks'},'Explicit source audio and track selection required')
            require(self._view.analysis_range is not None,'Inspect the actual source audio before generation')
            self._select_tracks(selection['tracks'])
            streams=[s for s in self._view.streams if s['stream_index']==selection['stream_index']]
            require(len(streams)==1 and type(selection['stream_index']) is int and selection['channels']==streams[0]['channels'],
                    'Select the sole actual global audio stream and all of its channels')
            expected_choice=self._selection_revision
            self.runtime.verify();f=e._facts;start,end=self._view.analysis_range
            chosen={'asset_id':f.source.id,'source_path':f.source.path,'source_sha256':f.source.sha256,
                'stream_index':selection['stream_index'],'channels':copy(selection['channels']),'mix':'unknown',
                'start_us':start,'end_us':end,'context_start_us':start,'context_end_us':end,
                'profile_path':self.runtime.profile[0],'profile_sha256':self.runtime.profile[1],
                'kit_revision':{'kit_id':f.moment.kit_id,'version':f.moment.kit_version}}
            prepared=self.preparer.prepare(chosen,self.directory/'prepared',cancel=cancel)
            if prepared.owner is not None:self._retained=prepared.owner
            if prepared.state=='cancelled' and not self.preparer.has_unsettled:
                return self._outcome('cancelled','Caption generation cancelled before submission. Reload captions when ready.')
            require(prepared.state=='complete' and not self.preparer.has_unsettled,'Audio preparation did not complete: '+str(prepared.error_code))
            if cancel is not None and cancel.is_set():return self._outcome('cancelled','Caption generation cancelled before submission. Reload captions when ready.')
            with CreatorLibrary(e.workspace.database) as library:
                request=prepared.load_request();tid=library.captions.create({k:request[k] for k in ('source','audio','range')})
                self.runner=DurableCaptionRunner(library,self.directory/'artifacts',self.client)
                job=self.runner.submit(prepared,transcript_id=tid,idempotency_key=str(uuid.uuid4()))
                actual=self.runner.run_next(job_id=job['id'],cancel=cancel)
                require(actual is not None and actual['id']==job['id'],'The selected caption job was not run')
                if actual['state']=='cancelled' and not self.runner.has_unsettled:
                    return self._outcome('cancelled','Caption generation cancelled. The earlier selected words remain unchanged.')
                require(actual['state']=='completed','Caption job is '+actual['state']+'. Its retained claim needs explicit attention.' )
                refs=self.runner.import_completed(job['id'])
                selector=MomentCaptionSelector(library,self.directory/'artifacts')
                selected=selector.select(f.moment.id,expected_moment_revision=f.moment.revision,
                    expected_selection_revision=expected_choice,job_id=job['id'],**refs)
                proposal=self._proposal(library,selected)
                self._selection_revision=selected['selection_revision']
            return self._outcome('proposal','Review these generated words. Accepted corrections remain unchanged.',proposal=proposal,repair=None,can_accept=True,can_generate=True)
        return self.dispatch(operation)

    def accept(self,token,job_id,transcript_id,hypothesis_id,alignment_id,*,expected_revision=0,tracks=None):
        def operation():
            e=self._admit(token);require(type(expected_revision) is int and expected_revision==0,'Initial acceptance requires base revision zero')
            if tracks is not None:self._select_tracks(tracks)
            require(self._tracks is not None,'Select the three existing native caption tracks first')
            with CreatorLibrary(e.workspace.database) as library:
                selected=library.caption_selections.selected(e._facts.moment.id,e._facts.moment.revision)
                require(selected is not None and all(selected[k]==v for k,v in {'job_id':job_id,'transcript_id':transcript_id,'hypothesis_id':hypothesis_id,'alignment_id':alignment_id}.items()),
                        'The explicitly selected proposal changed; reload before accepting')
                selector=MomentCaptionSelector(library,self.directory/'artifacts')
                _evidence(selector._read('requests',selected['request_ref']['id']+'.json'),selected['request_ref']['sha256'])
                _evidence(selector._read('results',selected['result_ref']['id']+'.json'),selected['result_ref']['sha256'])
                job=library.jobs.job(job_id)
                _evidence(selector._read('executions',job['output_set_id'],hypothesis_id,'receipt.json'),selected['receipt_sha256'])
                library.caption_selections.accept_selected(e._facts.moment.id,expected_moment_revision=e._facts.moment.revision,
                    expected_selection=selected,expected_revision=expected_revision)
            return self._remember_repair(self._repair_adapter().load(transcript_id,1))
        return self.dispatch(operation)

    def _remember_repair(self,view,*,checked=False):
        if view.state=='saved_uncertain':self._native_uncertain=True
        elif checked and view.state=='saved_applied' and not self._adapter.barrier.exists():self._native_uncertain=False
        return self._outcome(view.state,view.message,repair=view,
            can_generate=self._view.analysis_range is not None and not self.unsettled)

    def repair(self,token,action,view=None,**fields):
        def operation():
            self._admit(token,observation=action=='check');adapter=self._repair_adapter()
            specifications={'load':{'transcript_id','revision'},'reload':{'transcript_id'},'replace':{'word_id','text'},
                'retime':{'word_id','start_us','end_us'},'apply':set(),'check':set()}
            require(action in specifications and set(fields)==specifications[action],'Unknown or incomplete caption repair action')
            require(not self._native_uncertain or action=='check','Only Check editor can resolve this caption application')
            if action in ('load','reload'):
                require(self._view.repair is not None and fields['transcript_id']==self._view.repair.transcript_id,'Reload the selected accepted transcript only')
                result=adapter.load(**fields) if action=='load' else adapter.reload_saved(fields['transcript_id'])
            else:
                require(view==self._view.repair and view is not None,'Use the exact displayed saved revision')
                if action=='replace':result=adapter.save_spelling(view,fields['word_id'],fields['text'])
                elif action=='retime':result=adapter.save_timing(view,fields['word_id'],fields['start_us'],fields['end_us'])
                elif action=='apply':result=adapter.apply_saved(view)
                else:result=adapter.check_editor(view)
            return self._remember_repair(result,checked=action=='check')
        return self.dispatch(operation,observation=action=='check')

    def observe(self,*,cleanup=False):
        def operation():
            require(not self._native_uncertain,'Use Check editor for the exact uncertain saved revision')
            for owner in (self.preparer,self.runner or self.client):
                action=owner.cleanup_execution if cleanup else owner.observe_execution
                proof=action(wait_seconds=5 if cleanup else 0)
                require(proof.get('ceased') is True and proof.get('owner_unsettled') is False,'The original caption process, readers or guards remain unresolved')
            if self.runner is not None and self.runner.interrupted_job is not None:
                if cleanup:
                    claim=self.runner.interrupted_job
                    with CreatorLibrary(self.editor.workspace.database) as library:current=library.jobs.job(claim['id'])
                    self.runner.reconcile_execution(claim['id'],expected_revision=current['revision'])
                else:return self._outcome('blocked','Processes checked. Explicit cleanup must settle the retained interrupted claim; outputs will not be accepted.')
            require(self._retained is None or self._retained in (self.preparer,self.client,self.runner),'The retained capability requires its original explicit recovery owner')
            self._retained=None
            return self._outcome('ready','The original caption owner is settled. Reload captions before generating again.')
        return self.dispatch(operation,observation=True)

    def source_monitor(self,token,action,*,word_id=None):
        """Use Filmcraft's Source monitor and its conversion; no Python player or loop."""
        def operation():
            e=self._admit(token)
            require(action in ('show','play','stop'),'Unknown Source monitor action')
            validation=e._session.bridge.execute('clipline.validate',{})
            rows=[r for owner in validation['owners'] if owner['owner']==self._owner
                  for unit in owner['units'] for r in unit['roles'] if r['role']=='gameplay']
            require(rows and len({r['resolvedMediaItem'] for r in rows})==1,'One verified resolved native source is required')
            require(all(r['resolvedMediaPath']==e._facts.source.path for r in rows),'The native Source monitor source differs')
            item=rows[0]['resolvedMediaItem'];bridge=e._session.bridge
            if action=='show':
                when=e._facts.moment.source_in_us
                if word_id is not None:
                    require(self._view.repair is not None,'Load the accepted words first')
                    matches=[w for w in self._view.repair.words if w.id==word_id]
                    require(len(matches)==1 and matches[0].start_us is not None,'This word needs a source time before it can be shown')
                    when=matches[0].start_us
                bridge.execute('source.open',{'item':item})
                bridge.execute('source.setPlayhead',{'seconds':when/1000000})
            else:
                require(bridge.execute('source.inspect',{}).get('media')==item,'Show this verified source before requesting playback')
                bridge.call('ui.menu.invoke',{'id':'source.playback.'+action,'params':{}})
            require(bridge.execute('source.inspect',{}).get('media')==item,'The Source monitor binding changed')
            state=self._view.repair.state if self._view.repair is not None else ('proposal' if self._view.proposal else 'ready')
            self._view=replace(self._view,state=state,message='Source monitor updated in Filmcraft. Playback is not a bounded word audition.')
            return self._view
        return self.dispatch(operation)
