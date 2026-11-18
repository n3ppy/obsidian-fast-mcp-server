# caddy.d

Optional extra Caddy routes for this site. Every `*.caddy` file here is imported into
the site block of `../Caddyfile`, before the catch-all that sends everything else to the
MCP server. Use `handle` blocks with a matcher, for example:

```caddyfile
@app path /app /app/*
handle @app {
	reverse_proxy my-app:8000
}
```

Files you add here are ignored by git (they are specific to your server). After
changing them, reload Caddy: `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`.
