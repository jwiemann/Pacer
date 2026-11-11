# Re-apply after `paperclipai update`: doctor master.key win32 ACL check

`paperclipai update` wipes `node_modules`, destroying this patch alongside the
resolveSpawnTarget (.cmd spawn) and winpty quota-probe patches. Re-apply all
three after every update.

## Why

`secretsCheck` in the doctor warns whenever `(fs.statSync(keyFilePath).mode & 0o077) !== 0`.
On Windows libuv mirrors the user write bit to group/other and ignores NTFS ACLs,
so stat mode is always `666` (or `444` when read-only) — the warning is impossible
to clear with chmod/icacls. The real protection is the NTFS ACL, so the patch makes
the check ACL-aware on win32.

## One-time ACL restriction (real security fix)

```
icacls "%USERPROFILE%\.paperclip\instances\default\secrets\master.key" /inheritance:r
icacls "%USERPROFILE%\.paperclip\instances\default\secrets\master.key" /grant:r "%USERNAME%:F"
```
Expected: only `%USERDOMAIN%\%USERNAME%:(F)` remains.

## Code patch

Find (one occurrence, in `function secretsCheck`):

```js
  const keyMode = fs17.statSync(keyFilePath).mode & 511;
  const permissionWarning = (keyMode & 63) !== 0 ? `; key file permissions are ${keyMode.toString(8)} (run chmod 600 ${keyFilePath})` : "";
```

Replace with:

```js
  const keyMode = fs17.statSync(keyFilePath).mode & 511;
  let permissionWarning = (keyMode & 63) !== 0 ? `; key file permissions are ${keyMode.toString(8)} (run chmod 600 ${keyFilePath})` : "";
  if (permissionWarning && process.platform === "win32") {
    try {
      const acl = execFileSync("icacls", [keyFilePath], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
      const entries = [];
      for (const line of acl.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (!trimmed || !trimmed.includes(":(")) continue;
        const token = trimmed.split(/\s+/).pop();
        const idx = token.indexOf(":(");
        if (idx === -1) continue;
        const principal = token.slice(0, idx);
        const flags = [...token.slice(idx).matchAll(/\(([A-Z,]+)\)/g)].map((m) => m[1]);
        entries.push({ principal, flags });
      }
      const owner = `${process.env.USERDOMAIN ?? ""}\\${process.env.USERNAME ?? ""}`;
      const ownerOnly = entries.length === 1 && !entries[0].flags.includes("I") && entries[0].flags.every((f) => f === "F") && (entries[0].principal === owner || entries[0].principal === process.env.USERNAME);
      if (ownerOnly) {
        permissionWarning = "";
      }
    } catch {
    }
  }
```

Important gotchas:
- `dist/index.js` is an **ESM bundle** (esbuild, `"type": "module"`). Bare `require` is
  undefined there — "require is not defined" crashes the doctor mid-run. Reuse the
  module-scoped binding `execFileSync` (imported near line ~18410:
  `import { execFileSync } from "node:child_process";`, hoisted bundle-wide).
- Keep the bare `catch {}`: any throw must leave the warning in place (fail-closed).

## Verify

`paperclipai doctor` → `✓ Secrets adapter: ...` and summary `12 passed`, exit 0.
