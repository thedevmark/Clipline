"""The two explicitly supported native H.264 export recipes."""

SOFTWARE_RECIPE_ID = 'clipline.h264.software.v1'
GPU_RECIPE_ID = 'clipline.h264.gpu-auto.v1'


def export_recipe(recipe_id=SOFTWARE_RECIPE_ID):
    if recipe_id not in (SOFTWARE_RECIPE_ID, GPU_RECIPE_ID):
        raise ValueError('Unsupported pinned native export recipe.')
    acceleration = 'auto' if recipe_id == GPU_RECIPE_ID else 'off'
    return {
        'id': recipe_id, 'format': 'h264', 'range': 'entire',
        'gpuRendering': acceleration, 'hardwareEncoding': acceleration,
        'burnCaptions': True,
        'audio': {'codec': 'aac', 'sample_rate': 48000, 'channels': 2}, 'scale': 1,
    }
