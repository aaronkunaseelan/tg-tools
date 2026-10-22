# Reverse proxy deployment assets

This directory contains ready-to-edit reverse-proxy configs for exposing the local runtime-controller API.

## Files

- `nginx/runtime-controller.conf`
- `nginx/runtime-controller-local-tailscale.conf`
- `caddy/Caddyfile`

## Current assumptions

- app listens on `127.0.0.1:8787`
- upstream auth is still enforced by `RUNTIME_CONTROLLER_BEARER_TOKEN`
- TLS termination happens at nginx or Caddy when a real hostname and cert exist
- the live OCI deployment can also use the local+Tailscale-only nginx config on port 80

## Before enabling

1. choose the real public hostname
2. update the config file with that hostname
3. make sure DNS points to this machine
4. keep the API bound to localhost
5. keep `RUNTIME_CONTROLLER_BEARER_TOKEN` set unless you replace it with stronger auth

## Example nginx install path

### Public hostname + TLS-ready template

```bash
sudo cp ~/tools/runtime-controller/deploy/nginx/runtime-controller.conf /etc/nginx/sites-available/runtime-controller.conf
sudo ln -s /etc/nginx/sites-available/runtime-controller.conf /etc/nginx/sites-enabled/runtime-controller.conf
sudo nginx -t
sudo systemctl reload nginx
```

### Localhost + Tailscale-only live-style config

```bash
sudo cp ~/tools/runtime-controller/deploy/nginx/runtime-controller-local-tailscale.conf /etc/nginx/sites-available/runtime-controller
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -s /etc/nginx/sites-available/runtime-controller /etc/nginx/sites-enabled/runtime-controller
sudo nginx -t
sudo systemctl reload nginx
```

## Example Caddy install path

```bash
sudo cp ~/tools/runtime-controller/deploy/caddy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
