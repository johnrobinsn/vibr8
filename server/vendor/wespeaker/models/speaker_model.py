# Copyright (c) 2022 Hongji Wang (jijijiang77@gmail.com)
#               2024 Shuai Wang (wsstriving@gmail.com)
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0

# Slimmed for vibr8: only the ECAPA branch is kept (the only architecture
# referenced by the bsrnn_ecapa_vox1 checkpoint we ship).

import wespeaker.models.ecapa_tdnn as ecapa_tdnn


def get_speaker_model(model_name: str):
    if model_name.startswith("ECAPA_TDNN"):
        return getattr(ecapa_tdnn, model_name)
    raise ValueError(
        f"Vendored wespeaker only supports ECAPA_TDNN variants, got {model_name!r}"
    )
