# Third-party software notices

This list covers the material dependencies intentionally used by MDD Sim Gateway 1.0.0. Transitive package notices remain available in their corresponding package distributions.

| Component | Use | License | Source |
|---|---|---|---|
| fasferraz/SWu-IKEv2 | Modified SWu IKEv2/IPsec engine | GPL-3.0 | https://github.com/fasferraz/SWu-IKEv2 |
| sysmocom/Asterisk | IMS-AKA SIP, voice and SMS | GPL-2.0 | https://gitea.sysmocom.de/sysmocom/asterisk |
| phcoder/asterisk-docker | Reference build/integration | MIT | https://github.com/phcoder/asterisk-docker |
| mitshell/card | USIM and PC/SC helpers | GPL-2.0-or-later | https://github.com/mitshell/card |
| SagerNet/sing-box | Country-specific network exits | GPL-3.0-or-later | https://github.com/SagerNet/sing-box |
| estkme-group/lpac | Local eSIM profile assistant | AGPL-3.0-only | https://github.com/estkme-group/lpac |
| LudovicRousseau/PCSC | PC/SC middleware | BSD-3-Clause | https://github.com/LudovicRousseau/PCSC |
| LudovicRousseau/CCID | USB smart-card driver | LGPL-2.1-or-later | https://github.com/LudovicRousseau/CCID |
| JsSIP | Browser SIP/WebRTC client | MIT | https://github.com/versatica/JsSIP |
| React | Web interface | MIT | https://github.com/facebook/react |
| FastAPI | Control API framework | MIT | https://github.com/fastapi/fastapi |
| Android Open Source Project Carrier ID table | Offline MNO/MVNO identification data | Apache-2.0 | https://android.googlesource.com/platform/packages/providers/TelephonyProvider/ |

## Files that are not GPL-3.0-only

MDD Sim Gateway defaults to GPL-3.0-only, but two sets of files are derivative works of
upstream projects and keep the upstream license instead. A derivative of GPL-2.0-only code
cannot be relicensed to GPL-3.0, so these are tracked explicitly:

| Path | License | Derived from |
|---|---|---|
| `engine/patches/asterisk/mt_rpack_routing.py` | GPL-2.0-only | Asterisk `send_rpack()` (GPL-2.0-only) |
| `patches/ccid/*.patch` | LGPL-2.1-or-later | LudovicRousseau/CCID (LGPL-2.1-or-later) |

Both patch the upstream source at build time. The patched Asterisk runs as a separate
process inside the engine container and communicates with the GPL-3.0-only control plane
over AMI and HTTP only; the patched CCID driver is loaded by pcscd as a separate component.
No GPL-3.0-only code is linked into either. Redistributing a built image or host install
means also offering the corresponding modified Asterisk and CCID sources under their own
licenses.

MDD does not copy sing-box or lpac binaries into this source repository. The installer fetches pinned upstream releases/source and verifies published or reviewed SHA-256 values where a binary is downloaded. Full license texts and copyright notices are included in those upstream distributions.
