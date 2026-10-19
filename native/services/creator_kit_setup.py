"""Creator Kit authoring through the persistent library, on its background owner."""
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from native.core.creator_library import LibraryError, RevisionConflict, _descriptor
from native.services.workspace_library import WorkspaceLibrary


@dataclass(frozen=True)
class KitSetup:
    descriptor: dict
    template_path: str
    kit_id: str | None = None
    version: int | None = None

    @property
    def native_supported(self):
        return not self.descriptor['properties'] and set(self.descriptor['asset_refs']) == {'native_template'}


class CreatorKitSetup:
    def __init__(self, workspace: WorkspaceLibrary):
        self.workspace = workspace

    def load(self, kit_id, version):
        with self.workspace._library() as library:
            descriptor = library.kit(kit_id, version)
            asset = descriptor['asset_refs'].get('native_template')
            path = library.asset(asset).path if asset else ''
            return KitSetup(descriptor, path, kit_id, version)

    def save(self, *, name, vocabulary, profiles, template_path, kit_id=None, version=None):
        """Replace editable fields only; unowned properties and bindings stay canonical."""
        self.workspace.require_background()
        if not isinstance(template_path, str) or not template_path or Path(template_path).suffix.lower() != '.fcproj':
            raise LibraryError('Choose the shared Filmcraft editing template (.fcproj).')
        if not isinstance(profiles, list) or not profiles:
            raise LibraryError('Add an OBS scene and draw its Gameplay and Facecam regions.')
        with self.workspace._library() as library:
            if kit_id is None:
                if version is not None:
                    raise LibraryError('A new Kit cannot have a selected version.')
                descriptor = {'name': name, 'asset_refs': {}, 'properties': {},
                              'layout_profiles': [], 'vocabulary': vocabulary}
            else:
                if type(version) is not int or version < 1:
                    raise LibraryError('Select a saved Kit version before revising it.')
                descriptor = library.kit(kit_id, version)
                current = library.connection.execute('SELECT latest_version FROM kits WHERE id=?', (kit_id,)).fetchone()
                if current is None or current[0] != version:
                    raise RevisionConflict('This Kit has a newer version. Cancel and select its latest version before revising.')
            descriptor = deepcopy(descriptor)
            # Every old profile remains present. Unknown regions remain byte-for-byte
            # values even when this screen edits that profile's two supported regions.
            previous = {p['id']: p for p in descriptor['layout_profiles']}
            incoming = {p.get('id'): p for p in profiles if isinstance(p, dict)}
            if set(previous) - set(incoming):
                raise LibraryError('Existing OBS scene profiles cannot be removed from this setup screen.')
            merged = deepcopy(profiles)
            for profile in merged:
                if not isinstance(profile, dict):
                    raise LibraryError('Each OBS scene needs a saved layout profile.')
                prior = previous.get(profile.get('id'))
                if prior and not {'gameplay', 'facecam'} <= set(prior['regions']):
                    if profile != prior:
                        raise LibraryError('This saved OBS scene uses other region roles. Leave it unchanged and add another OBS scene.')
                    continue
                if prior:
                    foreign = {role: rect for role, rect in prior['regions'].items()
                               if role not in ('gameplay', 'facecam')}
                    profile['regions'] = {**deepcopy(foreign), **{
                        role: rect for role, rect in profile.get('regions', {}).items()
                        if role in ('gameplay', 'facecam')}}
                if not {'gameplay', 'facecam'} <= set(profile.get('regions', {})):
                    raise LibraryError('Draw both Gameplay and Facecam regions for each OBS scene.')
            descriptor.update(name=name, vocabulary=vocabulary, layout_profiles=merged)
            # Validate metadata before registering a file so invalid forms leave no assets.
            _descriptor(descriptor)
            try:
                old_id = descriptor['asset_refs'].get('native_template')
                old = library.asset(old_id) if old_id else None
                if old is not None and Path(template_path).absolute() == Path(old.path):
                    asset = library.verify_asset(old.id)
                else:
                    asset = library.register_asset(template_path)
                    # Deduplicated identities retain their canonical registered path.
                    asset = library.verify_asset(asset.id)
            except (OSError, LibraryError) as exc:
                raise LibraryError('The editing template is missing or changed. Restore its registered bytes, '
                                   'or choose a different .fcproj file. ' + str(exc)) from exc
            descriptor['asset_refs']['native_template'] = asset.id
            if kit_id is None:
                kit_id = library.create_kit(descriptor)
                version = 1
            else:
                version = library.revise_kit(kit_id, descriptor, expected_version=version)
            return KitSetup(library.kit(kit_id, version), asset.path, kit_id, version)
