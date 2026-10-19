"""Explicit configuration parsing only; no configured input is executed or imported."""
import copy
import json
from pathlib import Path
import pytest
from native.services.caption_runtime import load_caption_runtime


def manifest(tmp_path):
    program={'path':str(tmp_path/'authored.bin'),'sha256':'a'*64}
    return {'schema':1,'purpose':'Authored evaluation fixture','display_name':'Local CPU evaluation',
        **{name:copy.deepcopy(program) for name in ('profile','python','worker','ffmpeg','ffprobe')},
        'caption_projection_profile':{'id':'authored','rules':{'maxChars':18,'lines':1,'minDurationUs':0,
            'maxDurationUs':2000000,'gapFrames':0,'breakPauseUs':50000},'defaultSettings':'line:88% align:center'},
        'assets_verified':[dict(program,bytes=1,role='authored')],'observed_utc':'authored','limits':['No quality default']}


def test_explicit_runtime_parser_has_no_discovery_or_file_execution(tmp_path):
    file=tmp_path/'runtime.json';file.write_text(json.dumps(manifest(tmp_path)))
    runtime=load_caption_runtime(file)
    assert runtime.display_name=='Local CPU evaluation'
    assert runtime.projection['rules']['maxChars']==18
    assert not (tmp_path/'authored.bin').exists()


@pytest.mark.parametrize('change',[
    lambda value:value.update(schema=True),
    lambda value:value.update(unrecognized=True),
    lambda value:value['profile'].update(path='relative/profile.json'),
    lambda value:value['worker'].update(sha256='foreign'),
    lambda value:value['caption_projection_profile']['rules'].update(maxChars=0),
    lambda value:value['caption_projection_profile']['rules'].update(lines=True),
    lambda value:value['caption_projection_profile'].update(defaultSettings='unsafe\nsetting'),
],ids=['schema','unknown','relative','hash','wrapping','bool','control'])
def test_invalid_runtime_is_rejected_before_window_startup(tmp_path,change):
    value=manifest(tmp_path);change(value);file=tmp_path/'runtime.json';file.write_text(json.dumps(value))
    with pytest.raises((ValueError,TypeError)):load_caption_runtime(file)


def test_duplicate_runtime_keys_and_oversized_manifest_are_rejected(tmp_path):
    file=tmp_path/'runtime.json'
    for raw in ['{"schema":1,"schema":1}', ' '*65537]:
        file.write_text(raw)
        with pytest.raises(ValueError):load_caption_runtime(file)
