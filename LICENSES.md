# Dependency Licenses

TFR is distributed under the [MIT License](LICENSE).

The locked runtime dependency set was reviewed on 2026-11-10:

| Package | Locked version | License |
| --- | ---: | --- |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT |
| json-with-comments | 1.3.0 | MIT |
| openai | 3.46.0 | Apache-2.0 |
| plotext | 6.1.0 | MIT |
| prompt-toolkit | 3.0.53 | BSD |
| pywebpush | 2.5.0 | MPL-2.0 |
| pydantic | 2.14.3 | MIT |
| regex | 2026.11.7 | Apache-2.0 |
| retroflow | 0.10.0 | MIT |
| symspellpy | 6.10.2 | MIT |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 |
| aiosignal | 1.4.0 | Apache-2.0 |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| certifi | 2026.10.19 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| frozenlist | 1.8.0 | Apache-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore2 | 2.12.0 | BSD-3-Clause |
| httpx2 | 2.12.0 | BSD-3-Clause |
| http-ece | 1.2.1 | MIT |
| idna | 3.19 | BSD-3-Clause |
| jiter | 0.16.0 | MIT |
| networkx | 3.6.1 | BSD-3-Clause |
| multidict | 6.9.1 | Apache-2.0 |
| pillow | 13.0.0 | MIT-CMU |
| propcache | 0.5.4 | Apache-2.0 |
| py-vapid | 1.9.6 | MPL-2.0 |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic-core | 2.50.4 | MIT |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| truststore | 0.10.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| wcwidth | 0.8.3 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| yarl | 1.25.1 | Apache-2.0 |

The managed checkout wheel build additionally pins this build-only dependency:

| Package | Locked version | License |
| --- | ---: | --- |
| uv-build | 0.13.7 | MIT OR Apache-2.0 |

The PWA browser test harness additionally pins these development-only
dependencies, reviewed on 2026-09-28:

| Package | Locked version | License |
| --- | ---: | --- |
| playwright | 1.63.0 | Apache-2.0 |
| playwright-core | 1.63.0 | Apache-2.0 |

TinyFugue is GPLv2 software. TFR uses behavioral inspiration only and contains
no copied TinyFugue implementation code. TinyMUX source was consulted to verify
the NOSPOOF wire grammar; no TinyMUX source code is included.

Package distributions remain governed by their own license files. Regenerate
and review the locked runtime and build trees before release with
`uv tree --no-dev` and `uv tree --only-group build`.
