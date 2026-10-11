# Contributing

Help improve the catalog by suggesting a source, reporting a problem, or submitting a pull request.

## Suggest a patch source

Open an [issue](https://github.com/Jman-Github/Patch-Bundle-Registry/issues) with the repository URL, bundle format, and available release channels.

To submit a source directly, edit [config/sources.json](config/sources.json) and follow an existing entry for the same format and hosting provider.

- Add one entry per repository, using a unique, descriptive key.
- Use the normal repository URL in `patches`, such as `https://github.com/owner/repo` or `https://gitlab.com/group/project`. The refresh converts it to the provider's API URL automatically.
- Stable, prerelease, latest, and historical releases are discovered automatically; separate channel entries are unnecessary.
- For a self-hosted server, first map its hostname to `github`, `gitlab`, or `gitea` in `config/git-hosts.json`.
- Set `integrations: false` when no separate integrations APK is needed. For legacy bundles requiring one, set `integrations: true` and add `integration` with its normal repository URL immediately after it. Keep this enabled if historical releases still need integrations.
- Include `disabled: false` for an active source. Setting it to `true` disables every channel and format from that repository; an optional `unavailable_reason` explains why.

Include an example release artifact. Catalog snapshots are generated automatically; source submissions should change the inventory rather than generated data.

## Report a problem

Include the source, selected version, and steps to reproduce the issue. For compatibility problems, include your manager and app version.

## Code contributions

Follow the surrounding code style and include relevant validation with your pull request. Python tests run with `python -m pytest`; API tests run with `npm test`.

Keep the existing site layout and retain upstream attribution and license notices when changing adapted frontend files.

For questions, use [Discussions](https://github.com/Jman-Github/Patch-Bundle-Registry/discussions).

## Correct application metadata

Application names and icon URLs are cached from F-Droid's indexes and Google Play during refreshes. Packages with no public listing keep their package name.

To correct a name or provide metadata for an unlisted application, add an entry to `config/apps.json` keyed by its Android package name, with `app_name` and an optional HTTPS `icon_url`. These entries override provider metadata:

```json
{
  "com.example.app": {
    "app_name": "Example",
    "icon_url": "https://example.com/app-icon.png"
  }
}
```

## Preview website changes

Run `python scripts/preview_site.py --refresh-apps`, then open `http://localhost:8790`. Stop with Ctrl+C. The preview rebuilds its catalog under the ignored `internal/site-preview` directory and serves your current website files; it does not update the tracked database or publish anything.

The optional `--refresh-apps` flag populates cached F-Droid and bounded Google Play metadata. Omit it for faster restarts using cached names and icons. Apps without a public listing still use their package name and an initials icon.
