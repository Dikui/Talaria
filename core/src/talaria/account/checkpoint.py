"""模拟盘检查点只编码明确登记的数据字段，恢复时按字段契约校验。"""

import json

from talaria.foundation.types import ContractModel


class RuntimeCheckpoint(ContractModel):
    @classmethod
    def capture(cls, runtime) -> dict:
        state = cls.model_validate({name: getattr(runtime, "_" + name) for name in cls.model_fields})
        return state.model_dump(mode="json")

    @classmethod
    def restore(cls, runtime, payload: dict) -> None:
        state = cls.model_validate_json(json.dumps(payload))
        for name in cls.model_fields:
            setattr(runtime, "_" + name, getattr(state, name))
