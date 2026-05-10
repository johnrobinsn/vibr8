# Slimmed for vibr8: only BSRNN is kept (the only TSE separator we ship).

import wesep.models.bsrnn as bsrnn


def get_model(model_name: str):
    if model_name.startswith("BSRNN"):
        return getattr(bsrnn, model_name)
    raise ValueError(f"Vendored wesep only supports BSRNN, got {model_name!r}")
