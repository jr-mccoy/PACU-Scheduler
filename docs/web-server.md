# Running the web interface on an always-on Linux machine

The web interface (`python -m web`) serves the scheduler to phones and other
computers. It uses the same engine, database and settings file as the desktop
app. This guide sets it up on an always-on machine running Lubuntu (any recent
Ubuntu flavour works the same way). You reach it through
[Tailscale](https://tailscale.com), a free private network that only your own
devices and the people you invite can join.

The app has no login of its own, so it only listens on the machine itself
(`127.0.0.1`). Tailscale is the only way in: nothing is opened to the internet
and there is no router setup.

What you end up with:

- the scheduler running in the background from boot, restarted if it crashes;
- an address like `https://<machine-name>.tail1234.ts.net` that opens it on any phone or
  computer signed in to your Tailscale network;
- a copy of the database every night, with the newest 30 kept.

## 1. Install the prerequisites

Open a terminal (**Menu → System Tools → QTerminal**) and check the Python
version:

```bash
python3 --version
```

It needs **3.11 or newer**. Lubuntu 24.04 and later ship 3.12 or newer. On
22.04, which ships 3.10, install 3.11 as well and use `python3.11` wherever
this guide says `python3`.

```bash
sudo apt update
sudo apt install -y git curl python3-venv
# Lubuntu 22.04 only:
sudo apt install -y python3.11 python3.11-venv
```

## 2. Get the app and its dependencies

```bash
cd ~
git clone https://github.com/jr-mccoy/pacu-scheduler.git
cd pacu-scheduler
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-web.txt
```

If the repository is private, `git clone` asks you to sign in: give your
GitHub username and a
[personal access token](https://github.com/settings/tokens) as the password.

This also installs the desktop app's dependencies, so you can open the desktop
app on this machine against the same database (step 8).

## 3. Check the engine runs on this machine

```bash
.venv/bin/python scripts/demo.py
```

This schedules a made-up roster in a throwaway database and prints the result.
It should take about a minute or less. If it stops with `Illegal instruction`,
the installed OR-Tools needs newer processor features than this machine has.
Uninstall it (`.venv/bin/python -m pip uninstall -y ortools`) and run the demo
again: the scheduler then uses its built-in search instead. It is slower, and
can miss schedules the OR-Tools engine would find.

## 4. Bring your database over

If you already use the desktop app, the roster and history live in
`nurse_schedule.db`, in the folder you start the app from. Copy that file into
`~/pacu-scheduler/` on this machine, with the desktop app closed, for example
on a USB stick.

If you changed anything under **Settings** in the desktop app, also copy
`~/.nurse_scheduler/settings.json` to the same place on this machine. The web
interface uses those settings too.

From now on **this machine's copy is the real one**. Stop using the old copy
on the laptop. Two copies drift apart, and approving schedules in both loses
history.

If you are starting fresh, skip this step: the database is created empty and
you add the team under **Nurses**.

## 5. Start it as a service

```bash
deploy/install-service.sh
```

This asks for your password, then installs and starts two things:

- `pacu-scheduler-web`: the web interface, started at boot and restarted if
  it stops;
- `pacu-scheduler-backup.timer`: a copy of the database in `~/pacu-backups`
  every night at 02:30 (or at the next boot, if the machine was off).

Open <http://127.0.0.1:8080> in a browser on this machine to check it.

Useful commands:

```bash
systemctl status pacu-scheduler-web          # is it running?
journalctl -u pacu-scheduler-web -n 100      # its recent log
sudo systemctl restart pacu-scheduler-web    # after an update (step 9)
systemctl list-timers pacu-scheduler-backup  # when the next backup runs
```

## 6. Reach it from your phone with Tailscale

On this machine:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

`tailscale up` prints a link. Open it and sign in (a Google, Microsoft, Apple
or GitHub account works). Then publish the scheduler to your tailnet:

```bash
sudo tailscale serve --bg 8080
```

The first time, this asks you to enable HTTPS certificates for your tailnet
and gives you a link to do it. It then prints the address, something like
`https://<machine-name>.tail1234.ts.net`. `--bg` keeps it running across reboots.
`tailscale serve status` shows it again later.

On your phone, install the Tailscale app, sign in with the same account, and
open that address. Add it to your home screen so it opens like an app.

## 7. Invite your charge nurse

In the [Tailscale admin console](https://login.tailscale.com/admin/users), go
to **Users → Invite external users**, and invite them by email or with a link.
They install the Tailscale app, accept the invite, and open the same address.
The free Personal plan allows up to 6 users.

Everyone on your tailnet can use the scheduler, including approving
schedules, so only invite people who should.

## 8. Keep the machine awake

A sleeping machine can't answer. In **Preferences → LXQt Settings → Power
Management**, set the computer never to sleep or suspend (turning the screen
off is fine). To be sure, also switch off system sleep:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

The desktop app still works on this machine, against the same database.
Start it from the repository folder (`cd ~/pacu-scheduler && .venv/bin/python
main.py`) so it opens `nurse_schedule.db` there. Weekend history editing and
Settings are only in the desktop app for now. Avoid approving a schedule in
both at the same moment.

## 9. Updating

```bash
cd ~/pacu-scheduler
git pull
.venv/bin/python -m pip install -r requirements-web.txt
sudo systemctl restart pacu-scheduler-web
```

A schedule being generated when the service restarts is lost; generate it
again. Nothing is saved until an option is approved.

## Backups

`~/pacu-backups` is on the same disk as the database, so it protects against
mistakes, not against the disk dying. Now and then, copy the newest backup
somewhere else, such as your laptop or a USB stick. To restore one, stop the
service, copy it back over `nurse_schedule.db`, and start the service again:

```bash
sudo systemctl stop pacu-scheduler-web
cp ~/pacu-backups/nurse_schedule-2026-10-01.db ~/pacu-scheduler/nurse_schedule.db
sudo systemctl start pacu-scheduler-web
```
