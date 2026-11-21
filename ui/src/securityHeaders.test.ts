import { describe, expect, it } from "vitest";
import { apiOriginFor, contentSecurityPolicy, securityHeaders } from "../security-headers";

describe("security headers for the console", () => {
  it("keeps scripts same-origin and only lets the page call the API", () => {
    const csp = contentSecurityPolicy("https://api.example.test", false);
    expect(csp).toContain("script-src 'self'");
    expect(csp).not.toMatch(/script-src[^;]*unsafe/);
    expect(csp).toContain("connect-src 'self' https://api.example.test");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
  });

  it("sends HSTS and upgrades requests only over HTTPS", () => {
    expect(securityHeaders("http://localhost:8000", false)).not.toHaveProperty("Strict-Transport-Security");
    expect(securityHeaders("https://api.example.test", true)["Strict-Transport-Security"]).toContain("max-age=");
    expect(contentSecurityPolicy("http://localhost:8000", false)).not.toContain("upgrade-insecure-requests");
  });

  it("allows only 'self' when the API is a relative path on the same origin", () => {
    expect(apiOriginFor("/api")).toBe("");
    const csp = contentSecurityPolicy(apiOriginFor("/api"), true);
    // Exactly 'self': no extra origin, no empty or path-only source (both invalid in a CSP).
    expect(csp).toContain("connect-src 'self'; ");
    expect(csp).not.toMatch(/connect-src[^;]*\/api/);
    expect(csp).toContain("upgrade-insecure-requests");
  });

  it("reduces an absolute API URL to its origin and rejects other schemes", () => {
    expect(apiOriginFor("https://api.example.test/v1/")).toBe("https://api.example.test");
    expect(apiOriginFor("http://localhost:8000")).toBe("http://localhost:8000");
    expect(() => apiOriginFor("javascript:alert(1)")).toThrow();
    expect(() => apiOriginFor("//api.example.test")).toThrow();
  });
});
