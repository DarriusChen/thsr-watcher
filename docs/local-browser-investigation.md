# Local browser investigation — 2026-09-09

> Historical investigation, recorded on 2026-09-09–10. Statements below about
> blocked or pending verification describe that stage of the investigation.
> The complete CLI flow subsequently succeeded with `--compatibility` on
> 2026-10-02; see the [verification record](verification.md). Use
> [troubleshooting](troubleshooting.md) for current diagnostic instructions.
> Network settings and proposed next steps below are historical observations,
> not instructions to repeat those changes.

On 2026-09-09, VS-02 live verification was still blocked: the user observed
successful manual searches in regular and Incognito Chrome, but both manual and
automated searches in Playwright-launched Chrome failed with
`net::ERR_EMPTY_RESPONSE` on the reservation form's POST. These observations do
not establish anti-bot blocking.

## Read-only local checks

- macOS 26.5.2, arm64; Playwright 1.62.0.
- Installed and actually launched Chrome: 151.0.7922.176.
- Playwright's bundled Chromium version: 151.0.7922.34. Both share major 151;
  this does not guarantee compatibility, but no major-version mismatch was found.
- No proxy or custom certificate environment variables were set in the inspected
  Codex process. The user's terminal environment could differ.
- Unsandboxed `scutil --proxy` showed only `FTPPassive: 1`; no enabled HTTP,
  HTTPS, SOCKS, or PAC configuration was reported.
- No Chrome policy files were found in the three inspected system/managed
  preference locations. This does not rule out cloud or other policy sources.
- Urban VPN service was listed as disconnected.
- Surfshark WireGuard network extension was activated/enabled; two older versions
  were terminated and awaiting removal on reboot. Extension activation alone does
  not establish an active VPN connection or causation.
- IPv4 default route and the global primary interface both reported `ipsec0`.
  Active network interface listing included `ipsec0` and `en0`. The queried
  setup service did not identify the tunnel owner. Specific destination routes
  can differ from the default; no THSR destination lookup was made.

## Offline browser comparison

Two fresh Chrome contexts were inspected on about:blank with page networking
explicitly disabled. No THSR page was opened or search submitted.

| Setting | Browser defaults | Project locale/timezone |
| --- | --- | --- |
| language | zh-TW | zh-TW |
| languages | zh-TW, zh, en-US, en | zh-TW |
| timezone | Asia/Taipei | Asia/Taipei |
| User-Agent | Chrome/151.0.0.0 | Identical |
| webdriver | true | true |

The installed Playwright launcher source supplies its standard background,
extension, and debugging settings; the project does not add proxy, TLS, HTTP/2,
QUIC, or custom fingerprint arguments. Its source also adds `--no-sandbox` by
default. No flags were changed during this investigation. Reading live launch
arguments via CDP was unavailable because Browser.getBrowserCommandLine requires
an enable-automation flag that this runtime did not set; no flag was added to
work around that. Source inspection is not a capture of a regular Chrome launch.

## Assessment

No demonstrated software/configuration defect has yet been isolated. The active
`ipsec0` route warrants identifying the responsible VPN/network tool and its
routing/filtering configuration, with the owner, before further site checks.
Normal Chrome success means the tunnel alone is not proof of the failure's cause.
Do not rotate VPNs/proxies, import cookies, spoof browser attributes, or suppress
reservation POST errors. No network settings or application code were changed.

## Follow-up after the user disabled Surfshark

The user reported the same POST failure in the manual check after turning off
Surfshark. Read-only checks now show:

- Default IPv4 interface: `en0` (previously `ipsec0`).
- Effective `scutil --proxy`: HTTP/HTTPS explicit proxies disabled, but
  `ProxyAutoDiscoveryEnable: 1`, `ProxyAutoConfigEnable: 1`, and
  `ProxyAutoConfigURLString: http://wpad/wpad.dat`.
- Saved Wi-Fi settings from `networksetup`: Auto Proxy Discovery On; explicit
  auto-proxy URL null and its Enabled flag No.

This suggests the effective PAC setting comes from discovery rather than a
manually saved PAC URL. It does not demonstrate that the script was fetched,
that it selects a proxy for THSR, or that it causes the failure. No PAC was
fetched, no site requests made, and no network configuration changed. Determine
whether this Wi-Fi network requires managed proxy discovery before considering
a reversible configuration comparison. VPN removal alone did not fix the issue.

## Read-only follow-up — 2026-09-10

- The user confirmed Surfshark is currently on. The default IPv4 route is
  `ipsec0`; `scutil --nwi` reports a VPN server on that interface, with `en0`
  also present. This establishes an active tunnel, not the cause of the POST
  failure or the route selected for the specific THSR destination.
- Outside the sandbox, effective `scutil --proxy` currently reports only
  `FTPPassive: 1`; no effective PAC, HTTP, HTTPS, or SOCKS proxy is reported.
- Saved Wi-Fi Auto Proxy Discovery remains On. The saved auto-proxy URL is null
  and disabled. Explicit HTTP and HTTPS proxies are disabled; their stored
  loopback address/port does not mean an active proxy is in use.
- Surfshark's current network extension is activated/enabled; two older versions
  remain pending removal on reboot. Urban VPN is listed as disconnected.
- Installed Chrome is still 151.0.7922.176. No proxy or custom-certificate
  environment variables were found in the inspecting process. The user's
  terminal environment remains unverified.
- Sandboxed system-network queries were incomplete or denied; conclusions above
  use the successful read-only checks outside the sandbox. Process enumeration
  was denied and yielded no evidence about other running network applications.

Next: have the user disconnect Surfshark and identify whether the underlying
network is personal or managed, then read effective proxy/default-route settings
again without contacting THSR. The earlier VPN-off failure remains relevant:
VPN-on observations do not explain it. Do not disable automatic proxy discovery
until its role on the underlying network is understood. No network settings,
application code, browser flags, or site sessions were changed in this check.

### VPN off on the user's campus network

The user confirmed Surfshark was disconnected and identified the underlying
network as campus Wi-Fi. Successful read-only system checks show:

- Default route and the only reported active IPv4 interface are now `en0`.
- Effective proxy settings again enable automatic discovery and PAC at
  `http://wpad/wpad.dat`; explicit HTTP/HTTPS proxies remain disabled.
- Saved Wi-Fi settings still have automatic discovery On and the explicit PAC
  URL unset/disabled. This repeats the previously observed VPN-off state.

A single bounded read of the declared PAC URL, using the system resolver with
no explicit proxy, failed because `wpad` could not be resolved. No PAC content
was received or executed; no THSR request was made. This establishes that the
inspecting process could not obtain the advertised PAC at that moment. It does
not establish Chrome's effective proxy choice, cached PAC state, or the cause
of the reservation POST failure.

Leave campus proxy settings unchanged. A next controlled comparison can use the
user's own phone hotspot with VPN off, first checking the effective route/proxy
state, then one human-operated search if no access challenge is present. This
would test dependence on the campus network configuration, not prove a specific
PAC defect. If the same failure persists while ordinary Chrome succeeds, focus
subsequent investigation on browser/network diagnostics rather than repeated
site submissions or automation-concealment settings.

### Phone hotspot connected

After the user confirmed connection to their phone hotspot, read-only checks
showed the default gateway changed to `172.20.10.1` on `en0`, with no VPN
interface reported by `scutil --nwi`. However, effective proxy settings still
advertised automatic discovery and `http://wpad/wpad.dat`; explicit HTTP/HTTPS
proxies remained disabled. Thus this is a different network path but not yet a
comparison without the advertised PAC setting. The WPAD entry cannot currently
be attributed exclusively to the campus network. No settings were changed and
no site search was performed. A temporary, reversible automatic-discovery change
on the user's hotspot needs to preserve and restore the original Wi-Fi setting;
verify effective settings after changing it before drawing conclusions.

### Controlled hotspot comparison

With the user's approval, Wi-Fi Auto Proxy Discovery was temporarily turned
Off. The effective proxy state then contained no PAC URL and no enabled HTTP or
HTTPS proxy. Surfshark remained disconnected and the Mac remained on the phone
hotspot.

Under those conditions:

- A fully manual search in Playwright-launched Chrome did not reach a result or
  error page after submission. The user closed the unresponsive page; the
  resulting `net::ERR_ABORTED` therefore describes that closure, not the
  original cause of the stalled POST.
- A fully manual search in ordinary Chrome succeeded and displayed train
  results. No train was selected and the flow did not continue.

Wi-Fi Auto Proxy Discovery was restored to On immediately afterward and the
effective settings again advertised `http://wpad/wpad.dat`.

This controlled comparison rules out the campus path, active Surfshark tunnel,
and effective WPAD/PAC use as necessary causes of the failure. The observed
difference is now isolated to the Playwright-launched Chrome environment. It
does not distinguish a site-side automation restriction from a lower-level
interaction involving Playwright's browser launch or control channel. Further
compatibility experiments remain opt-in and must preserve the human CAPTCHA
handoff; importing a personal profile, reusing cookies, or automatically retrying
with different identities remains outside the project rules. VS-02 cannot be
considered live-verified until an experiment reaches a recognized result or
error page after one human CAPTCHA submission.

### Opt-in compatibility result

The browser layer gained an explicit `--compatibility` mode that uses installed
Chrome with Blink's `AutomationControlled` feature disabled. It remains off by
default and retains a fresh isolated context, one submission, and the external
human CAPTCHA boundary. It does not import a profile or cookies, change the
User-Agent, rotate network identity, or retry.

On the same phone hotspot with Surfshark disconnected and automatic proxy
discovery temporarily Off, the tool prepared the search and captured the CAPTCHA
successfully. The user entered the CAPTCHA and submitted directly in the browser;
selectable train results appeared. No train was selected. This demonstrates that
the compatibility launch environment removes the previously observed stalled
POST for a human-operated submission. It does not yet live-verify Playwright's
CAPTCHA fill, click, and result parser because the CLI remained at its CAPTCHA
prompt during the successful browser-side submission.

The pending CLI was then interrupted at the user's request. It reported a
resource-release error after the browser-side interaction, but the process ended
and the private CAPTCHA temporary directory was confirmed removed. Automatic
proxy discovery was restored to On and the effective WPAD/PAC state returned.
