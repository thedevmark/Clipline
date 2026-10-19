"""Remember explicit caption proposal choices in the existing Creator Library.

Artifact bytes must be loaded on the background thread before select_completed.
This store verifies their hash chain using catalog evidence; it does no media I/O,
process reconciliation, native application, or acceptance of machine words.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from native.core.accepted_transcript import _transaction


class CaptionSelectionError(ValueError):
    pass


class StaleCaptionSelection(CaptionSelectionError):
    pass


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise CaptionSelectionError("A canonical UUID is required") from error
    return value


def _integer(value, minimum=1):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise CaptionSelectionError("An integer inside the supported range is required")
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise CaptionSelectionError('A lowercase SHA256 is required')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CaptionSelectionError('Duplicate evidence keys are not allowed')
        result[key] = value
    return result


def _evidence(raw, expected_sha):
    if type(raw) is not bytes or not 0 < len(raw) <= 65536:
        raise CaptionSelectionError('Evidence requires at most 64 KiB of immutable bytes')
    if hashlib.sha256(raw).hexdigest() != _sha(expected_sha):
        raise CaptionSelectionError('The immutable caption evidence changed')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
        _digest(value)  # Reject NaN, Infinity and overflowing JSON numbers recursively.
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise CaptionSelectionError('Finite UTF-8 JSON evidence is required') from error
    if not isinstance(value, dict):
        raise CaptionSelectionError('Caption evidence must be an object')
    return value


def install_schema(connection):
    """Apply one additive domain migration without changing user_version."""
    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise CaptionSelectionError('The library must enable foreign keys')
    with _transaction(connection):
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='moment_caption_schema'").fetchone():
            row = connection.execute('SELECT version FROM moment_caption_schema WHERE singleton=1').fetchone()
            if row is None or row[0] != 1:
                raise CaptionSelectionError('Unsupported Moment caption schema')
            return
        statement = ''
        for line in Path(__file__).with_name('moment_caption_schema.sql').read_text(encoding='utf-8').splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                connection.execute(statement)
                statement = ''
        if statement.strip():
            raise CaptionSelectionError('Incomplete Moment caption migration')


class MomentCaptionSelections:
    def __init__(self, library):
        self.library = library
        self.connection = library.connection

    def selected(self, moment_id, moment_revision):
        """Return the last explicit choice for this exact revision, never a newest run."""
        row = self.connection.execute(
            'SELECT * FROM moment_caption_selections WHERE moment_id=? AND moment_revision=? '
            'ORDER BY selection_revision DESC LIMIT 1',
            (_uuid(moment_id), _integer(moment_revision))).fetchone()
        return self._selection(row) if row is not None else None

    def history(self, moment_id, moment_revision, *, after=0, limit=50):
        _uuid(moment_id); _integer(moment_revision); _integer(after, 0); _integer(limit)
        if limit > 200:
            raise CaptionSelectionError('At most 200 choices can be read at once')
        rows = self.connection.execute(
            'SELECT * FROM moment_caption_selections WHERE moment_id=? AND moment_revision=? '
            'AND selection_revision>? ORDER BY selection_revision LIMIT ?',
            (moment_id, moment_revision, after, limit + 1)).fetchall()
        return {'items': [self._selection(row) for row in rows[:limit]],
                'next_cursor': rows[limit - 1]['selection_revision'] if len(rows) > limit else None}

    @staticmethod
    def _selection(row):
        result = dict(row)
        result.pop('sequence')
        result['request_ref'] = {'id': result.pop('request_id'), 'sha256': result.pop('request_sha256')}
        result['result_ref'] = {'id': result.pop('result_id'), 'sha256': result.pop('result_sha256')}
        return result

    def select_completed(self, moment_id, *, expected_moment_revision, expected_selection_revision,
                         job_id, transcript_id, hypothesis_id, alignment_id,
                         request_bytes, result_bytes, receipt_bytes):
        """CAS one explicit proposal choice. Generation and selection never accept text."""
        _uuid(moment_id); _integer(expected_moment_revision); _integer(expected_selection_revision, 0)
        for identifier in (job_id, transcript_id, hypothesis_id):
            _uuid(identifier)
        if alignment_id is not None:
            _uuid(alignment_id)
        # Bound and decode evidence before the SQLite write transaction.
        job = self.library.jobs.job(job_id)
        settlement = self.library.jobs.settlement(job_id)
        try:
            if job['kind'] != 'caption' or job['state'] != 'completed' or settlement is None:
                raise CaptionSelectionError('Select a verified completed caption job')
            envelope = _evidence(request_bytes, job['input']['request_ref']['sha256'])
            result = _evidence(result_bytes, settlement['result_ref']['sha256'])
            receipt = _evidence(receipt_bytes, result['receipt_sha256'])
            request = envelope['request']
            request_sha = _digest(request)
            if set(envelope) != {'schema', 'transcript_id', 'accepted_revision', 'request'} or type(envelope['schema']) is not int or envelope['schema'] != 1:
                raise CaptionSelectionError('Unsupported caption execution envelope')
            if envelope['transcript_id'] != transcript_id:
                raise CaptionSelectionError('The request targets another transcript')
            for key, expected in (('job_id', job_id), ('claim_token', job['claim_token']),
                                  ('intent_sha256', job['intent_sha256']), ('output_set_id', job['output_set_id'])):
                if result[key] != expected or settlement[key] != expected:
                    raise CaptionSelectionError('Caption output belongs to another execution')
            if result['request_ref'] != job['input']['request_ref'] or result['run_id'] != request['run_id']:
                raise CaptionSelectionError('Caption output targets another request')
            if (result['outcome'], result['outputs_disposition'], result['process_disposition'], result['worker_state']) != ('completed', 'verified', 'stopped', 'complete'):
                raise CaptionSelectionError('Only completed verified caption outputs can be selected')
            if settlement['process_disposition'] != 'stopped' or settlement['outputs_disposition'] != 'verified':
                raise CaptionSelectionError('The completed execution is not settled')
            if receipt['state'] != 'complete' or receipt['run_id'] != request['run_id'] or receipt['hashes']['request'] != request_sha:
                raise CaptionSelectionError('The worker receipt targets another request')
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            if isinstance(error, CaptionSelectionError):
                raise
            raise CaptionSelectionError('Incomplete caption selection evidence') from error
        with _transaction(self.connection):
            # Recheck both mutable catalog heads under the same write lock.
            moment = self.library.moment(moment_id)
            if moment.revision != expected_moment_revision:
                raise StaleCaptionSelection('The Moment changed; reload its caption context')
            previous = self.selected(moment_id, expected_moment_revision)
            current_revision = previous['selection_revision'] if previous else 0
            if current_revision != expected_selection_revision:
                raise StaleCaptionSelection('The selected caption proposal changed')
            if self.library.jobs.job(job_id) != job or self.library.jobs.settlement(job_id) != settlement:
                raise StaleCaptionSelection('The completed caption job changed')
            self._validate_context(moment, job, request, envelope, transcript_id, hypothesis_id,
                                   alignment_id, receipt, request_sha)
            kit = self.library.kit_record(moment.kit_id, moment.kit_version)
            asset = self.library.asset(moment.source_asset_id)
            reference = job['input']['request_ref']; result_ref = settlement['result_ref']
            self.connection.execute(
                'INSERT INTO moment_caption_selections '
                '(moment_id,moment_revision,selection_revision,job_id,transcript_id,hypothesis_id,alignment_id,'
                'source_asset_id,source_sha256,kit_id,kit_version,kit_descriptor_sha256,request_id,request_sha256,'
                'profile_sha256,result_id,result_sha256,receipt_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (moment_id, expected_moment_revision, current_revision + 1, job_id, transcript_id,
                 hypothesis_id, alignment_id, asset.id, asset.sha256, moment.kit_id, moment.kit_version,
                 kit.descriptor_sha256, reference['id'], reference['sha256'], job['input']['profile_sha256'],
                 result_ref['id'], result_ref['sha256'], result['receipt_sha256']))
            selected = self.selected(moment_id, expected_moment_revision)
        return selected

    def accept_selected(self,moment_id,*,expected_moment_revision,expected_selection,expected_revision=0):
        """CAS the exact authenticated selection and initial accepted head together.

        The service loads and authenticates immutable artifacts before this call.
        This transaction contains only the existing catalog and word owners.
        """
        _uuid(moment_id);_integer(expected_moment_revision)
        if not isinstance(expected_selection,dict):
            raise CaptionSelectionError('The exact authenticated selected proposal is required')
        with _transaction(self.connection):
            moment=self.library.moment(moment_id)
            if moment.revision!=expected_moment_revision:
                raise StaleCaptionSelection('The Moment changed before acceptance')
            selected=self.selected(moment_id,expected_moment_revision)
            if selected is None or selected!=expected_selection:
                raise StaleCaptionSelection('The selected proposal changed before acceptance')
            job=self.library.jobs.job(selected['job_id'])
            settlement=self.library.jobs.settlement(selected['job_id'])
            kit=self.library.kit_record(moment.kit_id,moment.kit_version)
            asset=self.library.asset(moment.source_asset_id)
            if (job['kind']!='caption' or job['state']!='completed' or settlement is None
                    or job['input']['request_ref']!=selected['request_ref']
                    or settlement['result_ref']!=selected['result_ref']
                    or (asset.id,asset.sha256,moment.kit_id,moment.kit_version,kit.descriptor_sha256)
                       !=(selected['source_asset_id'],selected['source_sha256'],selected['kit_id'],selected['kit_version'],selected['kit_descriptor_sha256'])):
                raise StaleCaptionSelection('Caption acceptance pins changed')
            return self.library.captions._accept_initial(selected['transcript_id'],selected['hypothesis_id'],
                alignment_id=selected['alignment_id'],expected_revision=expected_revision,origin='human')

    def _validate_context(self, moment, job, request, envelope, transcript_id, hypothesis_id,
                          alignment_id, receipt, request_sha):
        try:
            identity = self.library.captions.identity(transcript_id)
            if {key: request[key] for key in ('source', 'audio', 'range')} != identity:
                raise CaptionSelectionError('The selected request differs from its transcript identity')
            source = identity['source']; interval = identity['range']
            asset = self.library.asset(moment.source_asset_id)
            if source['asset_id'] != asset.id or source['sha256'] != asset.sha256:
                raise CaptionSelectionError('The selected transcript uses another source')
            if not interval['start_us'] <= moment.source_in_us < moment.source_out_us <= interval['end_us']:
                raise CaptionSelectionError('The analysis range does not cover this Moment')
            kit_pin = {'kit_id': moment.kit_id, 'version': moment.kit_version}
            transcript_pin = None
            if envelope['accepted_revision'] is not None:
                revision = _integer(envelope['accepted_revision'])
                self.library.captions.revision(transcript_id, revision)
                transcript_pin = {'transcript_id': transcript_id, 'revision': revision}
            expected = {'assets': [{'asset_id': asset.id, 'sha256': asset.sha256}],
                        'kit': kit_pin, 'profile_sha256': _sha(request['profile_sha256']),
                        'range': interval, 'transcript': transcript_pin, 'snapshot': None, 'recipe_sha256': None}
            if request.get('kit_revision') != kit_pin or any(job['input'].get(key) != value for key, value in expected.items()):
                raise CaptionSelectionError('The caption job uses different source, Kit, profile or range pins')
            h = self.library.captions.proposal(transcript_id, hypothesis_id, 'hypothesis')
            if hypothesis_id != request['run_id'] or h['request_sha256'] != request_sha or _digest(h) != receipt['hashes']['hypothesis']:
                raise CaptionSelectionError('The hypothesis is not this completed output')
            if alignment_id is not None:
                a = self.library.captions.proposal(transcript_id, alignment_id, 'alignment')
                if a['hypothesis_run_id'] != hypothesis_id or a['request_sha256'] != request_sha or _digest(a) != receipt['hashes']['alignment']:
                    raise CaptionSelectionError('The alignment is not this completed output')
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, CaptionSelectionError):
                raise
            raise CaptionSelectionError('The caption proposal cannot be bound to this Moment') from error
