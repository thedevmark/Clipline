"""Apply one pinned accepted transcript through the native projection command.

No source-to-sequence mapping or cue grouping occurs here. The native editor
owns those operations and all three caption tracks in one undoable command.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import uuid

from native.services.caption_worker.client import write_once
from native.services.native_checkpoint import WorkingCopy
from native.services.filmcraft_bridge import NativeCommandError, OutcomeUnknown


class CaptionApplicationError(ValueError):
    pass


class AppliedOutcomeUncertain(CaptionApplicationError):
    def __init__(self,directory,message):
        self.directory=directory
        super().__init__(message)


@dataclass(frozen=True)
class AppliedCaptions:
    accepted: dict
    native_revision: int
    receipt_directory: Path


def _uint(value):
    if type(value) is not int or not 0<=value<2**64:
        raise CaptionApplicationError("A native unsigned integer is required")
    return value


def _pin(value):
    if not isinstance(value,dict) or set(value)!={"transcript_id","revision","payload_sha256"}:
        raise CaptionApplicationError("A complete native accepted pin is required")
    try:
        if str(uuid.UUID(value["transcript_id"]))!=value["transcript_id"]:raise ValueError()
    except (ValueError,TypeError,AttributeError) as exc:
        raise CaptionApplicationError("A canonical transcript UUID is required") from exc
    if _uint(value["revision"])==0 or not isinstance(value["payload_sha256"],str) or not re.fullmatch("[0-9a-f]{64}",value["payload_sha256"]):
        raise CaptionApplicationError("A native revision/payload hash is required")
    return dict(value)


def read_native_pin(bridge,owner):
    """Read the native hash; do not reproduce Rust JSON hashing in Python."""
    result=bridge.execute("metadata.get",{"item":_uint(owner)})
    try:
        if not isinstance(result,dict) or type(result["item"]) is not int or result["item"]!=owner:raise ValueError()
        metadata=result["metadata"]
        if not isinstance(metadata,dict):raise ValueError()
        text=metadata.get("clipline.acceptedCaptions.v1")
        if text is None:return None
        if not isinstance(text,str) or len(text.encode("utf8"))>8*1024**2:raise ValueError()
        return _pin(json.loads(text)["pin"])
    except (KeyError,ValueError,TypeError,RecursionError) as exc:
        raise CaptionApplicationError("Native caption metadata is invalid") from exc


def apply_accepted_captions(library,bridge,working,*,owner,transcript_id,revision,tracks,
                            expected_pin,receipts_directory,engine_version):
    """Verify source identity, then submit exactly one native accepted-word edit.

    A lost reply is never retried. A malformed reply or a source change after
    the command retains the response and requires reconciliation, not blind undo.
    This does not publish a ProjectStore checkpoint or infer library head.
    Invoke from the background owner; source verification reads the full asset.
    """
    if type(working) is not WorkingCopy:
        raise CaptionApplicationError("An explicit editable working copy is required")
    _uint(owner)
    if expected_pin is not None:expected_pin=_pin(expected_pin)
    if not isinstance(engine_version,str) or not engine_version.strip() or len(engine_version)>128:
        raise CaptionApplicationError("The qualified engine identity is required")
    if not isinstance(tracks,list) or len(tracks)!=3:
        raise CaptionApplicationError("Explicit managed bindings for all three formats are required")
    tracks=json.loads(json.dumps(tracks,allow_nan=False))
    projection=library.captions.projection_input(transcript_id,revision=revision)
    asset=library.verify_asset(projection["identity"]["source"]["asset_id"])
    if asset.sha256!=projection["identity"]["source"]["sha256"]:
        raise CaptionApplicationError("Accepted words belong to another registered source")
    validation=bridge.execute("clipline.validate",{})
    try:
        if not isinstance(validation,dict) or validation["valid"] is not True:raise ValueError()
        native_revision=_uint(validation["revision"])
        owners=validation["owners"]
        if not isinstance(owners,list) or not 1<=len(owners)<=64:raise ValueError()
        expected_links=[{"owner":_uint(row["owner"]),"revision":_uint(row["linksRevision"])} for row in owners]
        if len({row["owner"] for row in expected_links})!=len(expected_links):raise ValueError()
        matching=[row for row in owners if row["owner"]==owner]
        if len(matching)!=1:raise ValueError()
        units=matching[0]["units"]
        if not isinstance(units,list) or not 1<=len(units)<=4096:raise ValueError()
        sources=[]
        for unit in units:
            gameplay=[role for role in unit["roles"] if role["role"]=="gameplay"]
            if len(gameplay)!=1:raise ValueError()
            role=gameplay[0]
            if Path(role["resolvedMediaPath"]).resolve(strict=True)!=Path(asset.path).resolve(strict=True):
                raise CaptionApplicationError("Native media needs an explicit verified relink to the registered source")
            source={"sourceItem":_uint(role["sourceItem"]),"resolvedMediaItem":_uint(role["resolvedMediaItem"]),
                    "storedMediaIdentity":role["storedMediaIdentity"]}
            if not isinstance(source["storedMediaIdentity"],dict):raise ValueError()
            sources.append(source)
        if any(s["resolvedMediaItem"]!=sources[0]["resolvedMediaItem"] or s["storedMediaIdentity"]!=sources[0]["storedMediaIdentity"] for s in sources):
            raise CaptionApplicationError("Accepted projection requires one resolved native source")
    except (KeyError,TypeError,ValueError,OSError) as exc:
        raise CaptionApplicationError("Native linked source inventory is invalid: "+str(exc)) from exc
    directory=Path(receipts_directory).resolve()/str(uuid.uuid4())
    directory.mkdir(parents=True,exist_ok=False)
    params={"owner":owner,"expectedProjectPath":str(working.path.resolve(strict=True)),
            "expectedRevision":native_revision,"expectedLinksRevisions":expected_links,
            "expectedAcceptedRevision":expected_pin,"projectionInput":projection,
            "source":sources[0],"tracks":tracks}
    write_once(directory/"intent.json",{"schema":1,"engine":"filmcraft","engine_version":engine_version,
        "working_base_generation":working.base_generation,"working_base_revision":working.base_revision,
        "verified_asset":{"id":asset.id,"sha256":asset.sha256,"path":asset.path},"command":"clipline.acceptCaptions","params":params})
    try:response=bridge.execute("clipline.acceptCaptions",params)
    except OutcomeUnknown as exc:
        raise AppliedOutcomeUncertain(directory,str(exc)) from exc
    except NativeCommandError as exc:
        try:
            write_once(directory/"native-error.json",{"schema":1,"request_id":exc.request_id,
                "method":exc.method,"reason":exc.reason[:2000],"reason_truncated":len(exc.reason)>2000,
                "zero_side_effects_asserted":False})
        except (OSError,ValueError,TypeError) as receipt_error:
            raise AppliedOutcomeUncertain(directory,"The native error could not be retained; reconcile the request") from receipt_error
        raise
    try:write_once(directory/"response.json",response)
    except (OSError,ValueError,TypeError) as exc:
        raise AppliedOutcomeUncertain(directory,"The native reply could not be retained; reconcile before another edit") from exc
    try:
        if not isinstance(response,dict) or type(response["owner"]) is not int or response["owner"]!=owner:raise ValueError()
        pin=_pin(response["accepted"])
        if pin["transcript_id"]!=transcript_id or pin["revision"]!=revision:raise ValueError()
        returned_revision=_uint(response["revision"])
        if returned_revision<native_revision or type(response["captions"]) is not int or response["captions"]<0:raise ValueError()
        if library.verify_asset(asset.id)!=asset:raise ValueError("Source registration changed after the native edit")
    except (KeyError,TypeError,ValueError,OSError) as exc:
        raise AppliedOutcomeUncertain(directory,"The native command may have applied; reconcile the retained response: "+str(exc)) from exc
    try:
        write_once(directory/"applied.json",{"schema":1,"accepted":pin,"native_revision":returned_revision,
            "source_sha256":asset.sha256,"source_verification":"full registered source hash before and after command; no lifetime lock",
            "checkpoint_published":False})
    except (OSError,ValueError,TypeError) as exc:
        raise AppliedOutcomeUncertain(directory,"The edit applied, but its final receipt failed; reconcile the retained response") from exc
    return AppliedCaptions(pin,returned_revision,directory)
