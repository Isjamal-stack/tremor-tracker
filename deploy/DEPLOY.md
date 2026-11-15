# Deploy the dashboard

This installs the Tremor Tracker monitoring prototype on a fresh Ubuntu 24.04 server at Vultr. Caddy serves `https://tremortracker.us`, gets the certificate for you, and asks for a password before any page loads. Streamlit runs as the user `tremor`, from the repo's `.venv`, and listens on `127.0.0.1:8501`. Caddy on the same machine is the only client that talks to it.

The wristband still streams to the laptop. The laptop and the Pi keep the admin URL and the writer URL. This server only reads.

The service user is `tremor`. The app directory is `/home/tremor/tremor-tracker`. Those two strings are also in `tremor-dashboard.service`. If you change one, change the other.

## Before you start

- A Vultr instance with Ubuntu 24.04 and at least 1 GB of RAM. You have its public IPv4 address. Below, that address is `SERVER_IP`.
- The `deploy/` folder pushed to GitHub on the `dev` branch.
- The Tiger Data reader URL for the role `tremor_reader`. It ends in `sslmode=require`.
- A Gemini API key, and a long site password you can type into a browser.

`TIGER_URL` and `TIGER_WRITER_URL` stay off this server.

## 1. Point the domain at the server

At the DNS host for `tremortracker.us`, add an A record:

| Name | Type | Value |
| --- | --- | --- |
| `tremortracker.us` | A | `SERVER_IP` |

Give it a few minutes, then check from your laptop:

```bash
python3 -c "import socket; print(socket.gethostbyname('tremortracker.us'))"
```

The command should print `SERVER_IP`. If it says the name was not found, wait and run it again. Caddy cannot get a certificate until this matches.

## 2. Log in

```bash
ssh root@SERVER_IP
```

Type `yes` if SSH asks you to trust the host key. The commands below use `sudo`, so they also work if Vultr gave you a normal user with sudo.

Install the current security updates. If a screen asks about a config file, press Enter.

```bash
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt upgrade -y
```

If it prints `System restart required`, reboot, wait a minute, and SSH in again:

```bash
sudo reboot
```

## 3. Create the user

`tremor` runs the dashboard. This account has no password and no sudo.

```bash
sudo adduser --disabled-password --gecos "" tremor
id tremor
```

`id` should print a uid and the group `tremor`.

## 4. Install Python, git, and Caddy

Ubuntu 24.04's `python3` is 3.12. The dashboard runs on 3.12 or 3.13. Use 3.12. It is already the system Python, so you do not add another package archive.

```bash
sudo apt install -y python3 python3-venv python3-pip git ufw \
  debian-keyring debian-archive-keyring apt-transport-https curl
python3 --version
```

`python3 --version` should print `Python 3.12` and a patch number.

Add Caddy's own package archive, then install it. Caddy starts a placeholder page. The real site is step 10.

```bash
sudo rm -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo chmod o+r /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
caddy version
git --version
```

`caddy version` should print `v2` and a version number.

### Python 3.13 instead

Skip this if `python3 --version` already says 3.12 and you are happy with that.

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.13 python3.13-venv
python3.13 --version
```

In step 6, create the venv with `python3.13` in place of `python3`.

## 5. Clone the repo

```bash
sudo -u tremor -H git clone --branch dev \
  https://github.com/Isjamal-stack/tremor-tracker.git \
  /home/tremor/tremor-tracker
```

If GitHub says the repository was not found, or asks you to log in, the repo is private. Add a read-only deploy key, or clone with a personal access token, then rerun the command.

Confirm the deploy files are there:

```bash
ls /home/tremor/tremor-tracker/deploy
```

You should see `Caddyfile`, `DEPLOY.md`, and `tremor-dashboard.service`.

## 6. Create the virtual environment

```bash
sudo -u tremor -H python3 -m venv /home/tremor/tremor-tracker/.venv
sudo -u tremor -H /home/tremor/tremor-tracker/.venv/bin/pip install --upgrade pip
sudo -u tremor -H bash -c 'grep -v -e "^#" -e "^$" -e "^dl-myo" /home/tremor/tremor-tracker/requirements.txt | /home/tremor/tremor-tracker/.venv/bin/pip install -r /dev/stdin'
```

The server only runs the dashboard. `dl-myo` is the laptop Bluetooth library, and that line is left out. The install should include `numpy`, `pandas`, `psycopg2-binary`, `streamlit`, `altair`, `google-genai`, and `python-dotenv`.

## 7. Create the server .env

This file has four lines. Create it empty, locked down, then edit it as `tremor`:

```bash
sudo install -m 600 -o tremor -g tremor /dev/null /home/tremor/tremor-tracker/.env
sudo -u tremor -H nano /home/tremor/tremor-tracker/.env
```

Put these lines in the file. No `export`, and no quotes around the values.

```text
TIGER_READER_URL=postgres://tremor_reader:PASSWORD@HOST:5432/tsdb?sslmode=require
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash
TREMOR_TZ=America/New_York
```

Paste the `tremor_reader` connection string from Tiger as the value of `TIGER_READER_URL`. Keep `sslmode=require` at the end. Replace `your-gemini-api-key` with the Gemini key. `GEMINI_MODEL` matches `.env.example`. `TREMOR_TZ` is the zone the dashboard uses when it shows times. In nano, press Ctrl+O, Enter, then Ctrl+X.

If the database password contains `@`, `:`, `/`, `#`, `?`, `&`, or `%`, percent-encode that character in the URL. An `@` in the password is `%40`.

Check the names and the permissions. This prints the names only:

```bash
sudo -u tremor awk -F= 'NF { print $1 }' /home/tremor/tremor-tracker/.env
sudo stat -c '%a %U' /home/tremor/tremor-tracker/.env
```

The names should be `TIGER_READER_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, and `TREMOR_TZ`. The mode line should be `600 tremor`.

## 8. Open the firewall

OpenSSH is first, so this login stays up when the firewall turns on. The public ports are 22, 80, and 443. Caddy connects to Streamlit on the same machine, so Streamlit needs no public port.

In the Vultr panel, if this instance has a firewall group, add TCP 22, 80, and 443 there too. That firewall sits in front of the server.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status
```

`Status: active` should list OpenSSH, 80, and 443.

## 9. Enable the dashboard service

```bash
sudo cp /home/tremor/tremor-tracker/deploy/tremor-dashboard.service \
  /etc/systemd/system/tremor-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now tremor-dashboard
sudo systemctl status tremor-dashboard --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8501
```

`status` should say `active (running)`. The curl line should print `200`.

## 10. Set the site password and enable Caddy

The password covers the whole site. The pages show health data, and the report button calls Gemini with the key in `.env`.

Generate the hash. Type the site password twice. Caddy prints one long line that starts with `$2a$`.

```bash
caddy hash-password
```

That line is the hash. The browser password is the one you typed, and it never goes in a file. Leave off `--plaintext`, so the password stays out of shell history.

Copy the template into place and edit the live file:

```bash
sudo cp /home/tremor/tremor-tracker/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
```

Change `admin@tremortracker.us` to an inbox you read. Let's Encrypt uses it if a certificate is close to expiring. Replace `REPLACE_WITH_HASH` with the hash. Leave a single space between `tremor` and the hash. Do not add quotes. Save and exit.

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable caddy
sudo systemctl restart caddy
```

`validate` should end with `Valid configuration`. Caddy was already running from the package install. Restart makes it read this Caddyfile. The certificate is requested on that restart and renewed later by Caddy.

## 11. Check the logs

Dashboard, last 50 lines:

```bash
sudo journalctl -u tremor-dashboard -n 50 --no-pager
```

A healthy log includes `You can now view your Streamlit app in your browser.` and `URL: http://tremortracker.us:443`.

That URL line is the public name Streamlit records. The scheme in the log is `http` because Caddy holds the certificate. Open `https://tremortracker.us`. The curl command in step 9 is what shows that the process is on `127.0.0.1:8501`.

Caddy, last 50 lines:

```bash
sudo journalctl -u caddy -n 50 --no-pager
```

A healthy log includes `certificate obtained successfully` and `serving initial configuration`.

Follow both as they happen. Ctrl+C stops the follow and leaves the services running.

```bash
sudo journalctl -u tremor-dashboard -u caddy -f
```

From your laptop, this should print `401`. That is the password wall.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://tremortracker.us
```

Open `https://tremortracker.us` in a browser. Sign in as `tremor` with the password you typed into `caddy hash-password`. The page title is Tremor Tracker, and the caption says Monitoring prototype.

### If the logs show an error

- `ModuleNotFoundError`: rerun the pip command in step 6. It has to be the `.venv` pip.
- `Missing TIGER_READER_URL`: the `.env` file is missing that line, or it is not in `/home/tremor/tremor-tracker/.env`.
- A database connection or SSL error: the reader URL has to end in `sslmode=require`. Check the percent-encoding if the password has a reserved character.
- `start-limit-hit`: read the error above it, fix that, then run `sudo systemctl reset-failed tremor-dashboard` and `sudo systemctl start tremor-dashboard`.
- Caddy says a DNS or timeout problem: the python DNS command from step 1 has to print this server's IP, and `sudo ufw status` has to allow 80 and 443. Then `sudo systemctl restart caddy`.
- Caddy says the clock is wrong: `timedatectl` should say the clock is synchronized.
- The browser stays on "Please wait...": the public name in the service file has to be `tremortracker.us` on port `443`. That is `--browser.serverAddress` and `--browser.serverPort`.
- The browser keeps asking for a password: the username is `tremor`, and the password is the one you typed, not the hash.

## Done when

On the server:

```bash
systemctl is-enabled tremor-dashboard
systemctl is-enabled caddy
systemctl is-active tremor-dashboard
systemctl is-active caddy
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8501
```

The four `systemctl` lines print `enabled`, `enabled`, `active`, and `active`. curl prints `200`.

From your laptop, `curl -s -o /dev/null -w '%{http_code}\n' https://tremortracker.us` prints `401`. In a browser, the password lets you in, and the page shows Tremor Tracker.

## When you change the code

Push to `dev` first. On the server:

```bash
sudo systemctl stop tremor-dashboard
sudo -u tremor -H git -C /home/tremor/tremor-tracker pull --ff-only
sudo -u tremor -H bash -c 'grep -v -e "^#" -e "^$" -e "^dl-myo" /home/tremor/tremor-tracker/requirements.txt | /home/tremor/tremor-tracker/.venv/bin/pip install -r /dev/stdin'
sudo systemctl start tremor-dashboard
```

`/etc/caddy/Caddyfile` is the live copy. If `deploy/Caddyfile` changes, copy it again, put the email and the hash back, validate, and `sudo systemctl restart caddy`.

The server `.env` is not in git. Leave it there.
