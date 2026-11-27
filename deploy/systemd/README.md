# systemd units

Deployment templates for the dsh-kb backend. They are hand-written copies of the
units the author runs, with this machine's absolute paths replaced by the
`/opt/dsh/<repo>` convention and secrets removed.

| File | Installs as | Service |
| --- | --- | --- |
| `dsh-kb.service.example` | `/etc/systemd/system/dsh-kb.service` | FastAPI backend on `127.0.0.1:8816` |

`/opt/dsh/dsh-kb` is an example path, not a requirement: after `git clone` to
wherever you keep checkouts, replace it in `WorkingDirectory`, `EnvironmentFile`
and `ExecStart`. Data defaults to `<checkout>/server/data`, so a plain install
writes nothing outside the checkout.

## Install

```sh
sudo git clone https://github.com/frankfurtlin/dsh-kb /opt/dsh/dsh-kb
cd /opt/dsh/dsh-kb

/usr/bin/python3.11 -m venv .venv
.venv/bin/pip install -r server/requirements.txt

sudo cp deploy/systemd/dsh-kb.service.example /etc/systemd/system/dsh-kb.service
sudo systemctl daemon-reload
sudo systemctl enable --now dsh-kb
curl -s http://127.0.0.1:8816/healthz
```

Then mount the plugin (`dsh plugin add <path-to-your-checkout>`, or the Plugins
page) and refresh the DSH Web page; the host half proxies `/kb/*` to
`127.0.0.1:8816`.

## Why the `.service.example` suffix

systemd only loads files whose name ends in exactly `.service` from its unit
search path (`/etc/systemd/system`, `/run/systemd/system`, `/usr/lib/systemd/system`).
A `*.service.example` file inside a git checkout is therefore inert: it is not
scanned, it cannot be pulled in by a `systemctl enable`, and cloning the repo
never adds a unit. The copy step above is the only thing that activates it.

Same rule applies to the plugin: `dsh plugin add` symlinks the checkout into the
DSH profile, and the plugin's `package.json` declares no service files, so no
bundler or installer ever treats `deploy/` as units.

## Verification

```sh
# render the template with your real checkout path, then check it as a unit
sed 's#/opt/dsh/dsh-kb#/your/checkout#g' deploy/systemd/dsh-kb.service.example \
  > /tmp/dsh-kb.service
systemd-analyze verify /tmp/dsh-kb.service
```

`systemd-analyze verify` reports leftover warnings about missing dependencies
(for example `network.target` ordering on hosts without network-online) but must
not report syntax errors. Run it after editing the template.

## User and permissions

The unit ships `User=root` because that is what the author runs. For a
production host, create a dedicated account and give it the data directory:

```sh
sudo useradd --system --home /opt/dsh/dsh-kb --shell /usr/sbin/nologin dsh-kb
sudo chown -R dsh-kb:dsh-kb /opt/dsh/dsh-kb/server/data
# then set User=dsh-kb / Group=dsh-kb in the unit
```

Secrets belong in `server/.env.local` (mode `600`, owned by the service user),
never in the unit file itself.
