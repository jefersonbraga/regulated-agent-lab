/**
 * Security headers for the console's static pages, shared by `vite preview` and the nginx image
 * (docker/nginx.conf.template mirrors these values). No inline scripts exist, so scripts are
 * same-origin only. `apiOrigin` is the only other origin the page may call; an empty `apiOrigin`
 * (the API served on the same origin, e.g. `VITE_API_URL=/api` behind one reverse proxy) leaves
 * `connect-src 'self'` alone.
 */

/**
 * The origin the CSP must allow for `VITE_API_URL`: its origin when it is an absolute http(s) URL,
 * "" when it is a path on the page's own origin (e.g. "/api"), which `'self'` already covers.
 */
export function apiOriginFor(apiUrl: string): string {
  if (!/^https?:\/\//i.test(apiUrl)) {
    if (/^[a-z][a-z0-9+.-]*:|^\/\//i.test(apiUrl)) {
      throw new Error(`VITE_API_URL must be an http(s) URL or a path, got ${apiUrl}`);
    }
    return "";
  }
  return new URL(apiUrl).origin;
}

export function contentSecurityPolicy(apiOrigin: string, https: boolean): string {
  return [
    "default-src 'self'",
    "script-src 'self'",
    // The design system injects its theme and layer animations as <style> at runtime (content
    // varies by theme, so hashes would break on every upgrade): inline styles are allowed.
    // Scripts stay same-origin only, which is where the injection risk is.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:", // Vite inlines small font subsets as data: URLs
    apiOrigin ? `connect-src 'self' ${apiOrigin}` : "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
    ...(https ? ["upgrade-insecure-requests"] : []),
  ].join("; ");
}

export function securityHeaders(apiOrigin: string, https: boolean): Record<string, string> {
  return {
    "Content-Security-Policy": contentSecurityPolicy(apiOrigin, https),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    ...(https ? { "Strict-Transport-Security": "max-age=31536000; includeSubDomains" } : {}),
  };
}
