# Dependency Licenses

TFR is distributed under the [MIT License](LICENSE).

The locked runtime dependency set was reviewed on 2026-09-08:

| Package | Locked version | License |
| --- | ---: | --- |
| json-with-comments | 1.3.0 | MIT |
| openai | 3.9.0 | Apache-2.0 |
| plotext | 6.1.0 | MIT |
| prompt-toolkit | 3.0.53 | BSD |
| pydantic | 2.13.5 | MIT |
| retroflow | 0.10.0 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore2 | 2.12.0 | BSD-3-Clause |
| httpx2 | 2.12.0 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| jiter | 0.16.0 | MIT |
| networkx | 3.6.1 | BSD-3-Clause |
| pillow | 12.3.0 | MIT-CMU |
| pydantic-core | 2.46.5 | MIT |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| truststore | 0.10.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| wcwidth | 0.8.3 | MIT |

The managed checkout wheel build additionally pins this build-only dependency,
reviewed on 2026-09-17:

| Package | Locked version | License |
| --- | ---: | --- |
| uv-build | 0.12.15 | MIT OR Apache-2.0 |

TinyFugue is GPLv2 software. TFR uses behavioral inspiration only and contains
no copied TinyFugue implementation code. TinyMUX source was consulted to verify
the NOSPOOF wire grammar; no TinyMUX source code is included.

Package distributions remain governed by their own license files. Regenerate
and review the locked runtime and build trees before release with
`uv tree --no-dev` and `uv tree --only-group build`.
