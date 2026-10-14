# Catalog API

Browse and try requests in [Swagger UI](https://patch-bundles.jmancentral.com/api-docs/) or explore the GraphQL schema in [GraphiQL](https://patch-bundles.jmancentral.com/graphiql/).

API base: `https://patch-bundles.api.jmancentral.com`. [OpenAPI specification](https://patch-bundles.api.jmancentral.com/api.json).

## REST

| Method and path | Result |
| --- | --- |
| GET `/refresh-jobs` | Recent refresh runs, including queued, running and failed jobs. |
| GET `/api/v1/refresh/status/:jobId` | Refresh status; 202 while pending/running, 200 when finished. |
| GET `/health` | Catalog generation and counts. |
| GET `/sources`, `/sources/:id` | Registered sources, aliases, enabled status and repository metadata. |
| GET `/bundles`, `/releases`, `/bundles/:id` | Current and historical releases. |
| GET `/packages` | Compatible package names and recorded app versions. |
| GET `/patches`, `/bundles/:id/patches` | Patch descriptions and app compatibility. |
| GET `/availability` | Unavailable channels and retired source records. |
| GET `/api/v3/bundle?source_url=...&version=...&channel=...` | Manager import information. |
| GET `/api/reseam/bundle?source_url=...&version=...&channel=...` | Reseam Manager patch-source index with the verified publisher signing key. |
| GET `/api/v1/bundle/:id` | Legacy import using a numeric bundle ID. |
| GET `/api/v1/bundle/:owner/:repo/:version` | Legacy repository import for an exact tag or `latest`. |
| GET `/api/v2/bundle/:owner/:repo/latest?channel=...` | Legacy repository channel import. |

The API supports GitHub, GitLab, Codeberg, Gitea and configured self-hosted servers. GitLab subgroup paths and encoded API URLs normalize to repository URLs.

Disabled sources remain in source queries with `enabled: false` and an `unavailable_reason`. Their bundles and patches are excluded from browsing, relationships and manager imports; saved history is retained for re-enabling the source.

Removing every configuration entry for a repository deletes that source's indexed releases, bundles, patches and exclusive app/package records after the next successful publication. Apps shared with another configured source remain. Removing one alias or channel does not delete a repository still configured elsewhere. A missing upstream repository or failed check does not count as configuration removal.

For v3 imports, use an exact release tag or `version=latest`. Latest requires `channel=any|stable|prerelease`; `latest` and `dev` are accepted aliases. Channel values accept surrounding whitespace and any capitalization. Exact-version requests can omit channel. Latest imports and default website cards use the newest stable release and newest prerelease across retained history, independently of configured channel pointers.

V1 latest defaults to stable; `prerelease=true` selects prereleases. V2 requires a channel. V1/V2 owner and repository paths use recorded repository metadata, including current names after a rename and sources on other supported hosts. V1/V2 retain the upstream fields: `created_at`, `description`, `version`, `download_url` and `signature_download_url`. V3 adds bundle format, source information and metadata status. All manager import endpoints also return `icon_url` (the preferred bundle image) and `owner_avatar_url` (the fallback owner image); either can be null. Reseam returns these alongside its native `bundle` and `releases` fields.

Collection/detail endpoints also accept `/api/v1`, for example `/api/v1/patches`. REST records retain their catalog hash in `id`; `legacy_id` supplies the numeric ID used by legacy imports and GraphQL. Numeric IDs remain stable across refreshes and belong to this catalog. IDs from another catalog installation cannot be reused here.

Lists return `{data,total,limit,offset}`. Defaults: limit 25, offset 0. Limits: 1–100; offset: 0–10000. Use `sort` and `order=asc|desc`. Bundles accept `q`, `source` (ID, alias or URL), `source_url`, `ecosystem`, `channel`, `version`, `package` and `metadata_status`.

Patch search accepts `q`, exact `package`, `app_version`, `bundle_id`, source/ecosystem/channel filters and `include_unverified=true`. REST patch results default to verified metadata. A null version restriction means every app version is compatible. An explicitly empty version collection means no versions are supported and creates no package relationship. Legacy annotation defaults retain their unrestricted meaning.

Patch searches return one window of candidate shards. Follow `next_cursor` as `page_cursor`, keeping filters unchanged. Use offset to read additional rows within that window. Counts and sorting apply to the current window.

Bundle collections/details and GraphQL bundle records expose the same `icon_url` and `owner_avatar_url`. For Morphe, `icon_url` prefers `patches-bundle.png` at the root of the repository's default branch. Otherwise it uses the owner avatar, as ReVanced and Reseam do. Source metadata includes the discovered `bundle_icon_url`. Images are checked during the daily repository-metadata refresh; temporary upstream failures preserve the last known image. The website falls back to the owner avatar if the preferred image fails to load, then to initials.

Default website browsing shows current stable and prerelease bundles. Select a source to browse its indexed versions and load changelogs and patches on demand. The historical APIs below also expose retained release inventories, including releases without patch artifacts, on deployments that support them.

## GraphQL

Send JSON `{query,variables,operationName}` to `/graphql`, `/v1/graphql` or `/hasura/v1/graphql`. GET query parameters also work. GraphiQL provides schema documentation, completion and request execution.

The public read-only schema exposes all seven reference tables: `source`, `source_metadata`, `bundle`, `patch`, `package`, `patch_package` and `refresh_jobs`. Each has a `_by_pk` lookup; `patch_package` uses both `package_fk` and `patch_fk`. The six catalog tables expose `_aggregate` roots; refresh jobs follow the reference public role without aggregation.

Relationships work in both directions, including `source_metadatum`, source bundles, bundle patches and patch/package links. IDs and foreign keys use integers. `catalog_id` retains the hash used in REST and website links. The `release` alias and `patch(package_name:...,bundle_id:...,q:...)` shortcuts are also available.

Queries support variables, defaults, aliases, fragments, directives, introspection, logical and relationship filters, `distinct_on`, ordering with explicit null placement, and pagination. Aggregates provide counts (including distinct columns), nodes, min/max, sums, averages, variance and standard deviation. Array relationships support aggregate filters and ordering. Boolean aggregate filters accept `bool_and` and `bool_or` predicates with optional filters and distinct values. Boolean aggregates ignore null inputs and return null for an empty or all-null set.

Comparisons include equality, membership, null tests, ranges, LIKE/ILIKE (including escaped wildcards), their negations, regular expressions and SIMILAR TO. Nested boolean filters preserve PostgreSQL NULL semantics: negating a comparison does not include records with missing values; use `_is_null` to request them explicitly. Regex filters follow PostgreSQL advanced regular-expression syntax, including backreferences, lookahead/lookbehind, POSIX classes, word boundaries, embedded options and basic/extended/literal modes. SIMILAR TO uses SQL wildcards and whole-string matching. Character classes and simple case conversion use PostgreSQL's `en_US.utf8` locale by default, including international letters and word boundaries. ILIKE uses the same locale case conversion. The registry can select a precomputed locale profile; `C`/`POSIX` and `C.UTF-8` are also supported. Patterns remain limited to 512 characters and interpreted matching has a shared work budget per query. Expensive expressions return a budget error instead of blocking the API.

Bundle `created_at` and source metadata `repo_pushed_at` are strings, matching the upstream schema. Provider timestamp strings retain their timezone spelling and fractional seconds. They support string variables, LIKE/regex, text ordering, min/max and streaming cursors. V1/V2/V3 manager-import responses omit a trailing `Z` for upstream compatibility while retaining fractional seconds. Refresh-job `started_at` and `completed_at` remain timestamps and compare instants across timezone offsets.

Text range comparisons, sorting and string min/max use the same deterministic PostgreSQL collation. The default profile is libc `en_US.utf8` from glibc 2.36, including multi-character collation elements; `C`/`POSIX` and `C.UTF-8` use UTF-8 byte ordering. Precomputed patch statistics use the configured catalog locale as well.

Morphe compatibility accepts iterable collections, and absent patch descriptions remain null for GraphQL filtering. Patch names can be null or empty; the website displays “Unnamed patch” and preserves their descriptions and compatibility. A verified bundle can contain zero patches. Permanent bundle rejections set `need_patches_update` to false without marking metadata verified. Artifact or runtime changes make those bundles eligible again; temporary download or worker failures remain eligible for retry.

GraphQL exposes recorded patch metadata, including fallback and stale lists. Inspect `metadata_status` and `patch_metadata_version`, or filter for `metadata_status: {_eq: "verified"}`. This makes freshness explicit without hiding historical records.

```graphql
query Latest($url: String!) {
  bundle(where: {source: {url: {_eq: $url}}, is_latest: {_eq: true}}, limit: 5) {
    id catalog_id version bundle_type file_hash need_patches_update metadata_status
    source { id url source_metadata: source_metadatum { owner_name repo_name repo_stars } }
    patches(where: {metadata_status: {_eq: "verified"}}, limit: 30) {
      id name description
      patch_packages(limit: 50) { package { id name version } }
    }
    patches_aggregate { aggregate { count } nodes { name } }
  }
}
```

```graphql
{
  bundle_by_pk(id: 1) { version source { url } }
  source(where: {bundles_aggregate: {count: {predicate: {_gt: 0}}}}) { url }
  package_aggregate { aggregate { count(columns: [name], distinct: true) } }
}
```

Live subscriptions use WebSockets at the same GraphQL paths. Both `graphql-transport-ws` and the legacy `graphql-ws` protocol are supported. GraphiQL can run subscription operations. Clients receive an initial result and changed results as the registry refreshes, checked every second against the latest published checkpoint. Every evaluation keeps the same query and response limits. Each connection supports four operations; idle connections require initialization within 30 seconds.

Cursor streaming subscriptions are available as `bundle_stream`, `patch_stream`, `source_stream`, `source_metadata_stream`, `package_stream`, `patch_package_stream` and `refresh_jobs_stream`. Supply `batch_size` (1–100) and a `cursor` with `initial_value` and optional `ordering: ASC|DESC`. Each batch contains rows strictly after the previous cursor; the server retains progress across subscription polling and hibernation. Cursor columns need not appear in the selected response. Use a unique sortable cursor such as `id` to receive each record once. Non-unique cursors include boundary ties; a group exceeding 100 rows returns an error so records are not silently skipped. Existing query, shard and response budgets still apply. Empty polls do not emit a batch.

```graphql
subscription NewBundles {
  bundle_stream(batch_size: 25, cursor: [{initial_value: {id: 0}, ordering: ASC}]) {
    id version bundle_type source { url }
  }
}
```

The `is_latest` flag identifies the newest stable release and newest prerelease separately for each source, across bundle formats and retained history. Configured channel pointers remain available independently through `channels` and REST channel filters; manager latest imports use `is_latest`.

Promoted releases are reclassified without losing verified patch data. Standard browsing excludes deleted releases, drafts and releases without patch assets. The historical API preserves previously indexed published releases and marks missing upstream releases unavailable; it also includes releases without patch assets. Drafts are excluded. Temporary provider failures retain the last successful inventory. A latest prerelease import returns 404 when no eligible prerelease remains.

Refresh-job queries combine separate `BUNDLES` discovery and `PATCHES` extraction records with recent GitHub Actions refresh runs (`ALL`). Statuses are STARTED (including queued phases), COMPLETED and FAILED; cancellation is reported as FAILED. The status endpoint accepts a phase's UUID `job_id` or a numeric Actions run ID and returns the upstream fields `jobId`, `type`, `status`, `startedAt`, `completedAt` and `error`. Collection and GraphQL records retain snake_case field names. Legacy phase status follows catalog checkpoints. Cloud mode publishes phase records with the next verified generation; numeric Actions status remains live during a refresh. Live Actions status can be cached for 15 seconds to two minutes.

Mutations, triggering administrative jobs and database administration remain outside the public read-only API.

## Freshness and request limits

Release history is discovered independently of current channels. Historical patch extraction runs in background batches, so a newly discovered version may initially have a missing or unverified patch list. Re-extraction can update that status later. The website checks for a new generation every 15 seconds while visible; subscriptions check every second. Published updates can lag behind upstream releases while refreshes, extraction or caching are in progress.

A verified list was parsed from the matching binary artifact and has matching artifact and patch-list digests. Fallback metadata remains unverified. `need_patches_update` is true while extraction is missing, stale or unverified. Full release notes are available from bundle collections, detail and manager endpoints.

Each request uses one catalog generation. Responses support ETags and conditional GET, with a 60-second API cache lifetime. Refresh-status responses are not cached, and subscriptions evaluate against the latest published generation. Follow the generation checks below when reading multiple pages of history.

Queries have bounded input, output and work: 100 rows per selection (25 by default), 36 catalog files, 12 MiB catalog input, 2 MiB response and at most 24 patch shards per GraphQL scan. POST bodies are limited to 32 KiB; normal queries to depth 8, 300 fields and complexity 20,000. Standard schema introspection is allowed separately.

Narrow broad queries by source, bundle or package when a budget is exceeded. REST returns 422; GraphQL execution reports an error. These budgets apply to the public query surface, including compatibility queries.

Phase UUIDs remain queryable across catalog refreshes, including older completed jobs. Job collections and GraphQL fall back to published job records when live Actions status is temporarily unavailable; individual numeric Actions-run lookups still require live status. Collection queries retain their existing pagination and response limits.

Repository availability is reported in each source's `unavailable_reason`: permanent upstream errors include `404: Not Found`, `410: Gone` and `451: Unavailable For Legal Reasons`. The reason clears after release discovery and repository metadata both recover. A temporary upstream outage preserves previously published releases; the configured `disabled` switch remains separate.

Release replacements update the existing source/version/category record and preserve its public ID. Provider asset digests invalidate extracted patch metadata when an artifact changes at the same URL.

Bundle types are `ReVanced:v21`, `ReVanced:v22`, `Morphe:V1`, and `Reseam`. Use `bundle_type` to filter REST bundle and patch lists, or `ecosystem=reseam` for Reseam. The Reseam import endpoint uses the same source/version/channel selection as v3 and returns Reseam's native `bundle` and `releases` index. It returns 503 until the selected bundle's signing metadata has been verified.

## Applications

The catalog caches application names and icon URLs from F-Droid and IzzyOnDroid's bulk JSON indexes (v2, with v1 fallback), then Google Play listings for packages absent from those indexes. F-Droid names take priority when both indexes contain the same package; a missing icon can be filled from IzzyOnDroid or retained cached metadata. Metadata is refreshed by the catalog workflow; API requests and website visits do not query either provider for names. Unknown applications use their package name and a null icon. Icons are image URLs loaded by the client.

- `GET /apps`: paginated applications, including `package_name`, `app_name`, `icon_url`, `metadata_source`, supported `versions`, `unrestricted`, and matching `bundle_count`, `source_count`, and `patch_count`.
- `GET /apps/{packageName}`: application summary and paths to its bundles and patches.
- `GET /apps/{packageName}/bundles`: paginated bundle records with `app_compatibility` (versions, unrestricted compatibility, patch count, and metadata status).
- `GET /apps/{packageName}/patches`: the patch endpoint filtered to this exact package; supports the same shard cursors.
- `GET /bundles/{id}/apps`: paginated applications supported by that bundle.

These paths also accept the `/api/v1` prefix. Lists retain the existing limit of 100 records per page and other API budgets. Lists return `data`, `total`, `limit`, and `offset`.

App summaries and app-to-bundle lookups support `q` (application name or package), `source`/`source_url`, `ecosystem`, `bundle_type`, `version` (bundle tag), `channel`, `metadata_status`, `package` (fragment or `package@version`), and exact `app_version`. The `release` filter accepts `all` (default), `latest` (one newest release per source), `release`, or `prerelease`. By default only verified compatibility contributes; set `include_unverified=true` to include fallback patch metadata. Counts cover patches for the application in matching bundle versions, so the same patch can be counted in several releases. A version filter selects bundles supporting that version; patch counts include all patches for the app within those selected bundles.

GraphQL `package` records expose `app_name` and `icon_url`. Patch responses include `app_name` and `icon_url` alongside each package's existing `name` and `versions`. Bundle detail and v3 import responses include `apps` (first 100 applications), `app_count`, and an `apps_url` path for pagination. These fields appear once the new catalog indexes have been published; existing import fields remain compatible.

Application metadata is cached, so names and icons can lag behind provider changes. Temporary provider failures preserve the last known values; unknown applications keep their package names. Metadata changes do not require clients to change their API requests.

### Patch options

REST patch records and GraphQL `patch { options }` expose the extracted `options` array. Each option can include `key`, `title` or `name`, `description`, `type`, `required`, `default`, and `values` (named choices). Defaults preserve false, zero, and null. An empty array means the extracted patch declares no options; missing or unverified metadata does not establish that no options exist. Options are descriptive metadata; catalog requests do not apply or change them.

## Historical API

Historical endpoints are available on deployments using the D1 backend. Check `/history/sources` before relying on them; a legacy deployment returns 404 for these routes. Existing Android paths and GraphQL table roots keep their names, numeric IDs and response conventions.

| REST | GraphQL | Coverage |
| --- | --- | --- |
| `/history/releases` | `historical_releases` | Full retained published release inventory, including releases with no extractable artifact |
| `/history/releases/:id` | `historical_release(id:...)` | Original changelog, provider metadata, release/prerelease category, asset metadata and indexed bundle links |
| `/history/releases/:id/document` | `historical_release { document }` | Complete original release JSON in bounded byte chunks |
| `/history/sources` | `historical_sources` | Inventory completeness, counts, errors and verification timestamps |
| `/history/bundles` | `historical_bundles` | All retained indexed bundle metadata, including retired bundles |
| `/history/patches` | `historical_patches` | All retained indexed patches, options and compatibility status |
| `/history/packages` | `historical_packages` | Recorded package names and app versions |
| `/history/patch-packages` | `historical_patch_packages` | Historical patch/application links |
| `/history/apps` | `historical_apps` | Retained app metadata JSON |

Release history uses `limit=1..100` **per page**, with no total-history ceiling. Follow `next_cursor` as `after` until null. It sorts by publication date then stable release ID, with `order=asc|desc`. Keep filters and order unchanged between pages. Page size can be reduced if a response exceeds the size limit. A cursor pins the generation; HTTP 409 (or the equivalent GraphQL error) means restart pagination after publication. Do not concatenate pages from different generations.

Both release-history protocols accept `source_id`, `source` (repository URL), `bundle_id` (indexed catalog hash), `runtime` (recorded patcher-runtime value), exact `version`, `prerelease`, `availability`, inclusive publication bounds `from`/`to`, `order`, `limit` and `after`.

```http
GET /history/releases?source=https%3A%2F%2Fgithub.com%2Fowner%2Frepo&limit=100&order=asc
GET /history/releases?source=https%3A%2F%2Fgithub.com%2Fowner%2Frepo&limit=100&order=asc&after=ENCODED_NEXT_CURSOR
```

```graphql
query History($source: String!, $after: String) {
  historical_releases(source: $source, limit: 100, order: "asc", after: $after) {
    generation next_cursor
    data {
      id source_id source_url upstream_id name tag version is_prerelease
      created_at published_at updated_at changelog release_notes release_url
      availability binary_archived assets bundle_assets raw_metadata
      bundle_ids bundle_types runtimes metadata_document_url
    }
  }
}
```

REST returns the same release fields without a GraphQL selection set. Set `after` to the returned cursor in either protocol; IDs/cursors are interchangeable between them for identical filters. For original text, preserve JSON string contents without Markdown rewriting. Large records may replace inline changelog/assets/raw metadata with `metadata_document_url`; retrieve the complete JSON through the chunk route below.

Entity histories return a generation in REST. Send it as `generation` on subsequent pages to reject a catalog change with 409. In GraphQL, request `catalog_generation` alongside the first page and pass that value as the optional `generation` argument on subsequent historical entity/source/app roots; a mismatch returns an explicit GraphQL error. Restart the traversal when generations differ.

Entity histories use `limit<=100` and increasing `offset` without the legacy REST 10,000-offset ceiling. REST supports exact entity-specific filters `source_id`, `bundle_id`, `catalog_id`, `name`, `version`, `patcher_runtime`, `metadata_status`, and numeric `source_fk`, `bundle_fk`, `patch_fk`, `package_fk`. GraphQL uses table `where` filters and numeric ID ordering. REST uses catalog hashes plus `legacy_id`; GraphQL uses numeric IDs plus `catalog_id`, as existing APIs do. Historical GraphQL nested relationships include retired metadata. Use source completeness separately from extraction status.

```http
GET /history/bundles?source_id=SOURCE_HASH&limit=100&offset=0
GET /history/patches?bundle_id=BUNDLE_HASH&limit=100&offset=0
GET /history/patch-packages?patch_fk=NUMERIC_PATCH_ID&limit=100&offset=0
GET /history/packages?limit=100&offset=100
```

```graphql
query IndexedHistory($source: String!, $offset: Int!) {
  historical_bundles(where: {source_id: {_eq: $source}}, limit: 100, offset: $offset) {
    id catalog_id version description metadata_status
  }
  historical_patches(limit: 100, offset: $offset, order_by: {id: asc}) {
    id catalog_id name description options metadata_status
    bundle { catalog_id version }
    patch_packages { package { id name version } }
  }
  historical_apps(limit: 100, offset: $offset)
}
```

Historical entity roots use the D1-compatible filter/order subset (equality, membership, null/logical/relationship predicates and numeric order). Locale-sensitive text/regex/aggregate queries on existing roots retain the compatibility snapshot engine and its established budgets. Historical entity filters outside that subset return an explicit query error; existing roots retain their full compatibility expressions. Historical release pagination is independent of shard loading and does not have this fallback restriction.

For oversized original documents, use `offset` in **bytes** and `limit<=65536`. Both return `{encoding:"base64",data,total_bytes,next_offset}`. Decode each base64 chunk to bytes, concatenate bytes in order, then decode the complete UTF-8 JSON. Do not decode a partial UTF-8 character separately. Continue until `next_offset=null`.

```http
GET /history/releases/RELEASE_ID/document?offset=0&limit=65536
```

```graphql
{
  historical_release(id: "RELEASE_ID") {
    document(offset: 0, limit: 65536) { encoding data total_bytes next_offset }
  }
}
```

A whole page remains subject to the existing 2 MiB response ceiling. If a metadata-rich page exceeds it, reduce page size; original documents remain retrievable in chunks. An unavailable release retains its recorded original metadata, but an upstream asset URL may no longer work: binaries are not archived. Previously deleted, never-indexed metadata cannot be reconstructed.

## Refresh notifications

The optional Discord webhook sends rich embeds containing **New Bundles**, **Updated Bundles**, **New Apps**, and **New Patches**, omitting empty sections. Updated bundles show the previous version → current version, including patch changes at the same version. App and patch entries name their bundle; links lead to the app listing or bundle repository/release. Long lists continue across fields and messages within Discord's limits, with mentions disabled.

New apps mean newly supported packages for that bundle. Changing an app's cached display name or icon does not create a notification. Delayed extraction compares against the last verified metadata when available, avoiding announcements that label an entire existing patch list as new.
