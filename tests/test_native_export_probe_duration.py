"""Authored ffprobe timing regressions; no media is generated or decoded."""
from fractions import Fraction

import pytest

from native.services.native_export_client import ExportRuntimeError,verify_probe

TPS=254016000000

def captured(frames=240,num=60,den=1,duration_ticks=None):
    duration=Fraction(frames*den,num)
    return {'width':1920,'height':1080,'frames':frames,'frameRate':{'num':num,'den':den},
        'audio':{'sampleRate':48000,'channels':2},
        'range':{'startTicks':0,'durationTicks':int(duration*TPS) if duration_ticks is None else duration_ticks}}

def measured(f=None):
    f=f or captured();num=f['frameRate']['num'];den=f['frameRate']['den']
    # MP4 edit lists use a 1000Hz movie clock. The authored audio represents
    # padded AAC presentation, not packet-count times 1024.
    samples=f['range']['durationTicks']*48000//TPS
    audio=((samples+1023)//1024*1024)//48*48
    video=(Fraction(f['frames']*den,num)*1000).__floor__()
    return {'streams':[
        {'codec_type':'video','codec_name':'h264','width':f['width'],'height':f['height'],
         'sample_aspect_ratio':'1:1','r_frame_rate':f'{num}/{den}','avg_frame_rate':f'{num}/{den}',
         'nb_read_frames':str(f['frames']),'nb_frames':str(f['frames']),
         'time_base':'1/1000','start_pts':0,'duration_ts':video},
        {'codec_type':'audio','codec_name':'aac','sample_rate':'48000','channels':2,
         'time_base':'1/48000','start_pts':0,'duration_ts':audio,'nb_frames':str((samples+1023)//1024+1)}]}

@pytest.mark.parametrize('stream,field,value',[
    (1,'duration_ts',10),                  # Decodable but nearly empty audio.
    (1,'duration_ts',191952),              # A full movie tick of missing input.
    (1,'duration_ts',193024),              # A whole extra AAC access unit.
    (1,'start_pts',48),
    (1,'start_pts',-48),
    (0,'duration_ts',3983),                # Video lost more than movie rounding.
    (0,'duration_ts',4001),                # Video extended beyond captured frames.
    (0,'start_pts',1),
])
def test_timing_mismatch_is_not_completed_proof(stream,field,value):
    f=captured();p=measured(f);p['streams'][stream][field]=value
    with pytest.raises(ExportRuntimeError):verify_probe(p,f)

@pytest.mark.parametrize('stream,field,value',[
    (0,'duration_ts',True),(1,'duration_ts',192480.0),(0,'start_pts','0'),
    (1,'time_base','1/0'),(0,'time_base','NaN'),(0,'time_base','1/1000000000000000000000000'),
    (1,'duration_ts',None),(1,'time_base','-1/48000'),
])
def test_timing_metadata_requires_bounded_typed_facts(stream,field,value):
    f=captured();p=measured(f);p['streams'][stream][field]=value
    with pytest.raises(ExportRuntimeError):verify_probe(p,f)

@pytest.mark.parametrize('f',[
    captured(),captured(300),captured(210),captured(61,60000,1001),
    captured(240,duration_ticks=4*TPS-1),
])
def test_native_frame_rounding_and_bounded_aac_padding_are_valid(f):
    verify_probe(measured(f),f)

@pytest.mark.parametrize('field,value',[
    ('startTicks',1),('durationTicks',0),('durationTicks',4*TPS+1),
    ('durationTicks',int(Fraction(239,60)*TPS)),('durationTicks',True),
])
def test_capture_range_must_match_its_native_frame_span(field,value):
    f=captured();p=measured(f);f['range'][field]=value
    with pytest.raises(ExportRuntimeError):verify_probe(p,f)

def test_audio_packet_capacity_is_not_presented_duration():
    f=captured();p=measured(f)
    assert p['streams'][1]['duration_ts']==192480
    assert int(p['streams'][1]['nb_frames'])*1024!=192480
    verify_probe(p,f)

@pytest.mark.parametrize('field,value',[('sampleRate',48000.0),('channels',2.0)])
def test_native_audio_facts_do_not_coerce_float_values(field,value):
    f=captured();p=measured(f);f['audio'][field]=value
    with pytest.raises(ExportRuntimeError):verify_probe(p,f)
