// BetPawa virtual-sports API access.
//
// `page` is used only as an opaque fetch executor, so everything downstream of
// this module is testable without a browser.

export const LEAGUE_ID = '7794'; // English League

export const BETPAWA_HEADERS = {
    'X-Pawa-Brand': 'betpawa-cameroon',
    'deviceType': 'web',
    'Accept': 'application/json',
    'X-Pawa-Language': 'en',
};

export const SEASONS_ACTUAL_URL = 'https://www.betpawa.cm/api/sportsbook/virtual/v2/seasons/list/actual';
export const EVENTS_BY_ROUND_URL = (roundId) =>
    `https://www.betpawa.cm/api/sportsbook/virtual/v3/events/list/by-round/${roundId}`;

// Confirmed live: these endpoints sit behind Cloudflare with
// `Cache-Control: public, max-age=30` but `Vary: accept-encoding` only — NOT
// `Accept`. Since we deliberately send `Accept: application/json` (the real
// frontend sends `application/x-protobuf`), a plain request would share the
// same edge cache slot as real visitors' requests to the identical URL,
// letting our JSON response get served to them (or vice versa) for up to
// 30s — this was observed to genuinely break the live site's rendering.
// A unique cache-busting query param puts every request of ours in its own
// cache key that no real visitor's canonical-URL request will ever match,
// fully isolating our traffic from the shared cache.
function withCacheBuster(url) {
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}_cb=${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// Must run inside the real logged-in page: the API validates an
// X-Device-Fingerprint header tied to the actual browser, so a separate
// Node-side HTTP client with copied cookies is not reliable here.
export async function fetchJsonInPage(page, url) {
    return page.evaluate(
        async ({ url, headers }) => {
            const r = await fetch(url, { headers, cache: 'no-store' });
            if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
            return r.json();
        },
        { url: withCacheBuster(url), headers: BETPAWA_HEADERS }
    );
}

export async function fetchSeasonsActual(page) {
    const data = await fetchJsonInPage(page, SEASONS_ACTUAL_URL);
    return data.items || [];
}

export async function fetchRoundEvents(page, roundId) {
    const data = await fetchJsonInPage(page, EVENTS_BY_ROUND_URL(roundId));
    return data.responses || [];
}
