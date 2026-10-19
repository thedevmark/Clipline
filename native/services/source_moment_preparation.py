"""Prepare metadata Moments from admitted sources, without launching the editor."""
from dataclasses import asdict, dataclass
from fractions import Fraction
import json
from pathlib import Path

from native.core.creator_library import Asset, LibraryError, RevisionConflict, _fingerprint
from native.core.durable_jobs import _reference
from native.core.twitch_inbox import TwitchClip
from native.services.caption_worker.evidence import digest
from native.services.twitch_intake_client import PROFILE, classify_probe


@dataclass(frozen=True)
class SavedSource:
    clip: TwitchClip
    asset: Asset
    binding: dict
    facts: dict
    duration_us: int
    ticket: str


@dataclass(frozen=True)
class PreparationContext:
    source: SavedSource
    kits: object


@dataclass(frozen=True)
class KitChoice:
    kit_id: str
    version: int
    name: str
    descriptor_sha256: str
    profiles: tuple[tuple[str, str, int, int], ...]
    refusal: str = ''


def require(condition, message):
    if not condition:
        raise LibraryError(message)


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'The saved intake artifact contains duplicate fields.')
        result[key] = value
    return result


def invalid_constant(value):
    raise LibraryError('The saved intake artifact contains a non-finite value.')


def read_artifact(directory, kind, reference):
    """Intake artifact references identify canonical JSON, not file whitespace."""
    _reference(reference)
    root = Path(directory).resolve(strict=True)
    path = root / kind / (reference['id'] + '.json')
    require(not path.is_symlink() and path.resolve(strict=True).is_relative_to(root),
            'The saved intake artifact is outside its admitted directory.')
    with path.open('rb') as stream:
        encoded = stream.read(65537)
    require(len(encoded) <= 65536, 'The saved intake artifact exceeds its 64 KiB limit.')
    try:
        value = json.loads(encoded, object_pairs_hook=strict_object, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LibraryError('The saved intake artifact cannot be read. Restore its admitted bytes before preparing this clip.') from exc
    require(isinstance(value, dict) and digest(value) == reference['sha256'],
            'The saved intake artifact changed. Restore its admitted bytes before preparing this source.')
    return value


def validate_source_facts(facts):
    require(isinstance(facts, dict) and set(facts) == {'container','duration','video','audio','native_compatibility'}
            and facts['native_compatibility'] is False,
            'The intake result has no supported verified source facts.')
    video = facts['video']
    require(isinstance(video, dict), 'Verified source video facts are missing.')
    streams = [{'codec_type':'video', 'codec_name': video.get('codec'), **{
        key:value for key,value in video.items() if key != 'codec'}}]
    if facts['audio'] is not None:
        audio = facts['audio']
        require(isinstance(audio, dict), 'Verified source audio facts are invalid.')
        streams.append({'codec_type':'audio', 'codec_name':audio.get('codec'), **{
            key: str(value) if key == 'sample_rate' else value
            for key,value in audio.items() if key != 'codec'}})
    try:
        normalized = classify_probe({'streams':streams, 'format':{
            'format_name':facts['container'], 'duration':facts['duration']}})
    except ValueError as exc:
        raise LibraryError('The admitted intake facts do not match the supported file profile. This source cannot be prepared.') from exc
    require(normalized == facts, 'The intake source facts differ from the supported source contract.')
    duration = Fraction(facts['duration']) * 1_000_000
    # The conservative microsecond bound comes from the file's admitted format
    # duration. It is not a native frame clock or a Twitch/VOD time mapping.
    bound = duration.numerator // duration.denominator
    require(bound > 0, 'The verified file duration is shorter than one source microsecond.')
    return bound


class SourceMomentPreparation:
    def __init__(self, workspace):
        self.workspace = workspace
        self.directory = workspace.directory / 'source-intake'

    def _source(self, library, clip_id):
        binding, asset, job, settlement = library.admitted_twitch_source(clip_id)
        reference = {'id':binding['result_id'], 'sha256':binding['result_sha256']}
        result = read_artifact(self.directory, 'results', reference)
        request = read_artifact(self.directory, 'requests', job['input']['request_ref'])
        require(set(request) == {'schema','clip','profile','runtime'} and type(request['schema']) is int
                and request['schema'] == 1 and request['clip'] == binding['selected_metadata']
                and request['profile'] == PROFILE and digest(request['profile']) == binding['profile_sha256'],
                'The saved source request differs from its admitted clip or supported intake profile.')
        expected_keys = {'schema','job_id','claim_token','intent_sha256','output_set_id','selected',
                         'profile_sha256','runtime','media','source_facts','scope_proof','candidate_verified'}
        require(set(result) == expected_keys and type(result['schema']) is int and result['schema'] == 1
                and result['candidate_verified'] is True
                and result['selected'] == binding['selected_metadata'] and result['selected'] == request['clip']
                and result['runtime'] == request['runtime']
                and result['profile_sha256'] == binding['profile_sha256']
                and all(result[key] == job[field] for key,field in (
                    ('job_id','id'), ('claim_token','claim_token'),
                    ('intent_sha256','intent_sha256'), ('output_set_id','output_set_id'))),
                'The saved intake result belongs to a different clip, job, profile, or source.')
        proof = result['scope_proof']
        expected_execution = {'database':str(self.workspace.database.resolve()), 'job_id':job['id'],
            'claim_token':job['claim_token'], 'intent_sha256':job['intent_sha256'], 'output_set_id':job['output_set_id']}
        require(isinstance(proof,dict) and proof.get('binding') == expected_execution
                and proof.get('owner') == digest(expected_execution)
                and all(proof.get(key) is True for key in ('closed','processes_stopped','guards_released'))
                and all(proof.get(key) == [] for key in ('guarded_paths','guard_faults','cleanup_errors'))
                and isinstance(proof.get('scopes'),list) and len(proof['scopes']) == 2
                and all(isinstance(scope,dict) and scope.get('disposition') == 'stopped'
                        and type(scope.get('active_processes')) is int and scope['active_processes'] == 0
                        and scope.get('streams_closed') is True for scope in proof['scopes']),
                'The saved intake result has no matching stopped transfer and probe evidence.')
        media = result['media']
        require(isinstance(media,dict) and set(media) == {'path','sha256','bytes'}
                and media['sha256'] == asset.sha256 and type(media['bytes']) is int and media['bytes'] == asset.bytes,
                'The saved source fingerprint differs from its intake result.')
        expected_path = self.directory / 'media' / job['output_set_id'] / 'source.mp4'
        require(isinstance(media['path'],str) and Path(media['path']).is_absolute()
                and Path(media['path']) == expected_path
                and not expected_path.is_symlink()
                and expected_path.resolve(strict=True).is_relative_to(self.directory.resolve(strict=True)),
                'The intake source path differs from its owned output set.')
        duration_us = validate_source_facts(result['source_facts'])
        try:
            require(library.verify_asset(asset.id) == asset, 'The canonical shared source changed during verification.')
        except (OSError, LibraryError) as exc:
            raise LibraryError('The saved source is missing or changed. Restore its admitted bytes before preparing this clip.') from exc
        if expected_path != Path(asset.path):
            _, sha256, size = _fingerprint(expected_path)
            require(sha256 == asset.sha256 and size == asset.bytes, 'The intake source bytes changed after admission.')
        ticket = digest({'binding':binding, 'asset':asdict(asset), 'facts':result['source_facts']})
        return SavedSource(TwitchClip(**binding['selected_metadata']), asset, binding,
                           result['source_facts'], duration_us, ticket)

    def inspect(self, clip_id):
        with self.workspace._library() as library:
            try:
                source = self._source(library, clip_id)
            except OSError as exc:
                raise LibraryError('The saved intake result or source file is missing. Restore it before preparing this clip.') from exc
        return PreparationContext(source, self.workspace.page('kits'))

    def kit(self, kit_id, version):
        with self.workspace._library() as library:
            record = library.kit_record(kit_id, version)
            descriptor = library.kit(kit_id, version)
            refusal = ''
            if descriptor['properties'] or set(descriptor['asset_refs']) != {'native_template'}:
                refusal = 'This Kit version has properties or asset roles the native factory cannot apply. Choose another Kit version.'
            return KitChoice(kit_id, version, descriptor['name'], record.descriptor_sha256,
                tuple((p['id'],p['scene_key'],p['source_width'],p['source_height']) for p in descriptor['layout_profiles']), refusal)

    def versions(self, kit_id, selected_version, after=0):
        page = self.workspace.page('versions', after, context=kit_id)
        # Keep the explicitly selected version available even when older versions
        # occupy the first bounded page. No latest alias is substituted at Save.
        selected = self.kit(kit_id, selected_version)
        return page, selected

    def create(self, *, clip_id, expected_source_ticket, title, source_in_us, source_out_us,
               kit_id, kit_version, profile_id, scene_key, expected_kit_sha256):
        with self.workspace._library() as library:
            try:
                source = self._source(library, clip_id)
                require(source.ticket == expected_source_ticket,
                        'The selected source facts changed. Cancel and inspect the saved source again.')
                descriptor = library.kit(kit_id, kit_version)
                require(not descriptor['properties'] and set(descriptor['asset_refs']) == {'native_template'},
                        'This Kit version cannot be applied by the native factory. Choose another version.')
                try:
                    library.verify_asset(descriptor['asset_refs']['native_template'])
                except (OSError, LibraryError) as exc:
                    raise LibraryError('The selected Kit template is missing or changed. Restore it, or select another Kit version.') from exc
                return library.create_twitch_moment(title=title, source_in_us=source_in_us, source_out_us=source_out_us,
                    kit_id=kit_id, kit_version=kit_version, profile_id=profile_id, scene_key=scene_key,
                    expected_kit_sha256=expected_kit_sha256, expected_binding=source.binding,
                    expected_asset=source.asset, verified_source_facts=source.facts)
            except OSError as exc:
                raise LibraryError('A saved source, intake artifact, or Kit template is missing. Restore it before creating this Moment.') from exc
