Vendored from https://github.com/bupticybee/TexasSolver branch `console` at commit
6dfb65b4d7ed081da509e8d8c3d82138c4708267 (2026-08-19) on 2026-10-10. License AGPL-3.0 (see LICENSE):
private, local use only; the binary is run as a subprocess by `pn` (pn/pn/solve/texas.py).

Dropped from the copy: .git, .idea, benchmark/, imgs/, the Colab notebook, ext/*/doc|docs|test|tests,
ext/googletest/googlemock, resources/{outputs,gametree,yamls,python,bash}. Local changes are listed below;
keep them additive so upstream merges stay easy.

Local changes:

- 2026-10-10 (Plan 7, T1): `src/tools/CommandLineTool.cpp` accepts `set_raise_limit <n>` (bets per street,
  before `build_tree`; upstream hard-codes 4). Rebuild with tools/build_texassolver.sh.
