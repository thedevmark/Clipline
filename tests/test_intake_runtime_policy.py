import pytest
from tests.test_intake_runtime import manifest, save
from native.services import intake_runtime as subject


def test_dot_inventory_entry_rejects_with_configuration_value_error(tmp_path):
    value=manifest(tmp_path)
    value['code']['.']='c'*64
    with pytest.raises(ValueError):
        subject.load_intake_runtime(save(tmp_path,value))