const fs = require("fs");
const path = require("path");

const distDir = "/app/dist";

// ── Patch 1: fallback message guard ──────────────────────────────────────────
// Ensure stale assistant error messages are not shown when falling back to a
// different provider. Guards the error message path so that only messages from
// the current provider/model context are surfaced.
//
// ≥ 2026.8.1: params.timedOut/idleTimedOut replaced by
//             params.terminal.kind/source; gained a providerOwner field.
//             Verified the underlying bug (lastAssistant's formatted error
//             used regardless of whether its provider/model still matches
//             the current fallback context) is still present at this
//             version — not upstream-fixed, still needs this patch.
// ≥ 2026.6.7: bundle renamed pi-embedded-* → embedded-agent-*; function gained
//             authMode and idleTimedOut params — target the new signature.
// ≥ 2026.5.7: upstream extracted the inline expression into a dedicated
//             resolveAssistantFailoverErrorMessage() function — target that.
// < 2026.5.7: the expression was inline as `const message = ...` — keep as
//             last-resort fallback for older base images.

const allBundles = fs
  .readdirSync(distDir)
  .filter((name) => /^pi-embedded-.*\.js$|^embedded-agent-.*\.js$/.test(name));

const target = allBundles.find((name) => {
  const src = fs.readFileSync(path.join(distDir, name), "utf8");
  return src.includes("resolveActiveErrorContext");
});

if (!target) {
  console.warn("[hotfix] WARNING: resolveActiveErrorContext not found in any pi-embedded/embedded-agent bundle — skipping runtime patch");
} else {
  const filePath = path.join(distDir, target);
  let source = fs.readFileSync(filePath, "utf8");

  function replaceOne(label, pattern, replacement) {
    const next = source.replace(pattern, replacement);
    if (next === source) {
      console.warn(`[hotfix] WARNING: target not found (already patched or version changed): ${label}`);
      return false;
    }
    source = next;
    return true;
  }

  // Tried newest-signature-first; each attempt only runs once the ones
  // before it have failed, and none run again once one has succeeded —
  // `source` only ever contains one signature at a time, so once a match
  // is found the older patterns can't match it anyway, but skipping them
  // outright avoids printing confusing "not found" warnings for patterns
  // that were never going to apply.
  let fallbackGuardPatched = false;

  // ≥ 2026.8.1: params.timedOut/idleTimedOut → params.terminal.kind/source;
  // gained a providerOwner field passed through to formatAssistantErrorText.
  fallbackGuardPatched = replaceOne(
    "fallback message guard (resolveAssistantFailoverErrorMessage with terminal.kind/providerOwner)",
    /function resolveAssistantFailoverErrorMessage\(params\) \{\n\tconst timeoutFailure = params\.terminal\.kind === "timeout" && params\.terminal\.source !== "observation";\n\treturn \(params\.lastAssistant \? formatAssistantErrorText\(params\.lastAssistant, \{\n\t\tcfg: params\.config,\n\t\tsessionKey: params\.sessionKey,\n\t\tprovider: params\.activeErrorContext\.provider,\n\t\tproviderOwner: params\.providerOwner,\n\t\tmodel: params\.activeErrorContext\.model,\n\t\tauthMode: params\.authMode\n\t\}\) : void 0\) \|\| params\.lastAssistant\?\.errorMessage\?\.trim\(\) \|\| \(timeoutFailure \? "LLM request timed out\." : params\.rateLimitFailure \? "LLM request rate limited\." : params\.billingFailure \? formatBillingErrorMessage\(params\.activeErrorContext\.provider, params\.activeErrorContext\.model, params\.authMode\) : params\.authFailure \? "LLM request unauthorized\." : "LLM request failed\."\);\n\}/,
    `function resolveAssistantFailoverErrorMessage(params) {
	const timeoutFailure = params.terminal.kind === "timeout" && params.terminal.source !== "observation";
	const assistantErrorContextMatchesInFallback = (!params.lastAssistant?.provider || params.activeErrorContext.provider === params.lastAssistant.provider) && (!params.lastAssistant?.model || params.activeErrorContext.model === params.lastAssistant.model);
	const rawFallbackErrorMessage = assistantErrorContextMatchesInFallback ? params.lastAssistant?.errorMessage?.trim() : void 0;
	return (params.lastAssistant && assistantErrorContextMatchesInFallback ? formatAssistantErrorText(params.lastAssistant, {
		cfg: params.config,
		sessionKey: params.sessionKey,
		provider: params.activeErrorContext.provider,
		providerOwner: params.providerOwner,
		model: params.activeErrorContext.model,
		authMode: params.authMode
	}) : void 0) || rawFallbackErrorMessage || (timeoutFailure ? "LLM request timed out." : params.rateLimitFailure ? "LLM request rate limited." : params.billingFailure ? formatBillingErrorMessage(params.activeErrorContext.provider, params.activeErrorContext.model, params.authMode) : params.authFailure ? "LLM request unauthorized." : "LLM request failed.");
}`
  );

  // ≥ 2026.6.7: function signature added authMode and idleTimedOut
  if (!fallbackGuardPatched) fallbackGuardPatched = replaceOne(
    "fallback message guard (resolveAssistantFailoverErrorMessage with authMode/idleTimedOut)",
    /function resolveAssistantFailoverErrorMessage\(params\) \{\n\tconst timeoutFailure = params\.timedOut \|\| params\.idleTimedOut;\n\treturn \(params\.lastAssistant \? formatAssistantErrorText\(params\.lastAssistant, \{\n\t\tcfg: params\.config,\n\t\tsessionKey: params\.sessionKey,\n\t\tprovider: params\.activeErrorContext\.provider,\n\t\tmodel: params\.activeErrorContext\.model,\n\t\tauthMode: params\.authMode\n\t\}\) : void 0\) \|\| params\.lastAssistant\?\.errorMessage\?\.trim\(\) \|\| \(timeoutFailure \? "LLM request timed out\." : params\.rateLimitFailure \? "LLM request rate limited\." : params\.billingFailure \? formatBillingErrorMessage\(params\.activeErrorContext\.provider, params\.activeErrorContext\.model, params\.authMode\) : params\.authFailure \? "LLM request unauthorized\." : "LLM request failed\."\);\n\}/,
    `function resolveAssistantFailoverErrorMessage(params) {
	const timeoutFailure = params.timedOut || params.idleTimedOut;
	const assistantErrorContextMatchesInFallback = (!params.lastAssistant?.provider || params.activeErrorContext.provider === params.lastAssistant.provider) && (!params.lastAssistant?.model || params.activeErrorContext.model === params.lastAssistant.model);
	const rawFallbackErrorMessage = assistantErrorContextMatchesInFallback ? params.lastAssistant?.errorMessage?.trim() : void 0;
	return (params.lastAssistant && assistantErrorContextMatchesInFallback ? formatAssistantErrorText(params.lastAssistant, {
		cfg: params.config,
		sessionKey: params.sessionKey,
		provider: params.activeErrorContext.provider,
		model: params.activeErrorContext.model,
		authMode: params.authMode
	}) : void 0) || rawFallbackErrorMessage || (timeoutFailure ? "LLM request timed out." : params.rateLimitFailure ? "LLM request rate limited." : params.billingFailure ? formatBillingErrorMessage(params.activeErrorContext.provider, params.activeErrorContext.model, params.authMode) : params.authFailure ? "LLM request unauthorized." : "LLM request failed.");
}`
  );

  if (!fallbackGuardPatched) {
    // ≥ 2026.5.7, < 2026.6.7: resolveAssistantFailoverErrorMessage without authMode/idleTimedOut
    fallbackGuardPatched = replaceOne(
      "fallback message guard (resolveAssistantFailoverErrorMessage)",
      /function resolveAssistantFailoverErrorMessage\(params\) \{\n\treturn \(params\.lastAssistant \? formatAssistantErrorText\(params\.lastAssistant, \{\n\t\tcfg: params\.config,\n\t\tsessionKey: params\.sessionKey,\n\t\tprovider: params\.activeErrorContext\.provider,\n\t\tmodel: params\.activeErrorContext\.model\n\t\}\) : void 0\) \|\| params\.lastAssistant\?\.errorMessage\?\.trim\(\) \|\| \(params\.timedOut \? "LLM request timed out\." : params\.rateLimitFailure \? "LLM request rate limited\." : params\.billingFailure \? formatBillingErrorMessage\(params\.activeErrorContext\.provider, params\.activeErrorContext\.model\) : params\.authFailure \? "LLM request unauthorized\." : "LLM request failed\."\);\n\}/,
      `function resolveAssistantFailoverErrorMessage(params) {
	const assistantErrorContextMatchesInFallback = (!params.lastAssistant?.provider || params.activeErrorContext.provider === params.lastAssistant.provider) && (!params.lastAssistant?.model || params.activeErrorContext.model === params.lastAssistant.model);
	const rawFallbackErrorMessage = assistantErrorContextMatchesInFallback ? params.lastAssistant?.errorMessage?.trim() : void 0;
	return (params.lastAssistant && assistantErrorContextMatchesInFallback ? formatAssistantErrorText(params.lastAssistant, {
		cfg: params.config,
		sessionKey: params.sessionKey,
		provider: params.activeErrorContext.provider,
		model: params.activeErrorContext.model
	}) : void 0) || rawFallbackErrorMessage || (params.timedOut ? "LLM request timed out." : params.rateLimitFailure ? "LLM request rate limited." : params.billingFailure ? formatBillingErrorMessage(params.activeErrorContext.provider, params.activeErrorContext.model) : params.authFailure ? "LLM request unauthorized." : "LLM request failed.");
}`
    );
    if (!fallbackGuardPatched) {
      // < 2026.5.7: inline const message pattern
      fallbackGuardPatched = replaceOne(
        "fallback message guard (inline const message)",
        /const message = \(params\.lastAssistant \? formatAssistantErrorText\(params\.lastAssistant, \{[\s\S]*?\}\) : void 0\) \|\| params\.lastAssistant\?\.errorMessage\?\.trim\(\) \|\| \(params\.timedOut \? "LLM request timed out\." : params\.rateLimitFailure \? "LLM request rate limited\." : params\.billingFailure \? formatBillingErrorMessage\(params\.activeErrorContext\.provider, params\.activeErrorContext\.model\) : params\.authFailure \? "LLM request unauthorized\." : "LLM request failed\."\);/,
        `const assistantErrorContextMatchesInFallback = (!params.lastAssistant?.provider || params.activeErrorContext.provider === params.lastAssistant.provider) && (!params.lastAssistant?.model || params.activeErrorContext.model === params.lastAssistant.model);
                                const rawFallbackErrorMessage = assistantErrorContextMatchesInFallback ? params.lastAssistant?.errorMessage?.trim() : void 0;
                                const message = (params.lastAssistant && assistantErrorContextMatchesInFallback ? formatAssistantErrorText(params.lastAssistant, {
                                        cfg: params.config,
                                        sessionKey: params.sessionKey,
                                        provider: params.activeErrorContext.provider,
                                        model: params.activeErrorContext.model
                                }) : void 0) || rawFallbackErrorMessage || (params.timedOut ? "LLM request timed out." : params.rateLimitFailure ? "LLM request rate limited." : params.billingFailure ? formatBillingErrorMessage(params.activeErrorContext.provider, params.activeErrorContext.model) : params.authFailure ? "LLM request unauthorized." : "LLM request failed.");`
      );
    }
  }

  // Only write/log success if one of the attempts above actually matched —
  // previously this ran unconditionally, so a version where every pattern
  // had drifted still printed "Patched..." and rewrote the file unchanged,
  // silently masking exactly the failure this warns about now. Caught this
  // updating to 2026.8.1: all three older patterns missed (correctly
  // warned), but the misleading unconditional log made that easy to miss
  // in build output before this fix.
  if (fallbackGuardPatched) {
    fs.writeFileSync(filePath, source, "utf8");
    console.log(`Patched OpenClaw runtime bundle: ${filePath}`);
  } else {
    console.warn(
      `[hotfix] WARNING: fallback message guard patch did NOT apply to ${filePath} — ` +
      "none of the known signatures matched. File left unmodified. " +
      "Update hotfix-openclaw-runtime.js with a new pattern for this version."
    );
  }
}

// ── Patch 2: errors bundle — add GitHub Copilot premium-allowance failover ──
// When Copilot exhausts the gpt-5.4 premium quota it returns an error like:
//   "You have exceeded your premium request allowance. We have automatically
//    switched you to GPT-4.1 which is included with your plan."
// classifyFailoverReason() doesn't recognise this, so OpenClaw never triggers
// the configured gpt-4.1 / ollama fallbacks.  Classify it as "rate_limit" so
// the fallback chain fires automatically.
//
// Locate the errors bundle that contains PROVIDER_SPECIFIC_PATTERNS — there may
// be multiple errors-*.js shards in newer releases, so search all of them.
const allErrorsBundles = fs.readdirSync(distDir).filter((n) => /^errors-.*\.js$/.test(n));
const errorsBundle = allErrorsBundles.find((n) => {
  const src = fs.readFileSync(path.join(distDir, n), "utf8");
  return src.includes("PROVIDER_SPECIFIC_PATTERNS");
});
if (!errorsBundle) {
  console.warn("[hotfix] WARNING: PROVIDER_SPECIFIC_PATTERNS not found in any errors bundle — skipping Copilot patch");
} else {
  const errorsFile = path.join(distDir, errorsBundle);
  let errorsSource = fs.readFileSync(errorsFile, "utf8");
  // Use a regex so minor whitespace variations in the bundle don't break the match.
  // Two patterns are injected:
  //   1. Premium-allowance quota message → rate_limit  (triggers fallback to gpt-4.1 / ollama)
  //   2. Token-exchange HTTP 404/403    → rate_limit  (triggers fallback past all Copilot models
  //      to ollama when the PAT has no active Copilot subscription or is expired)
  const injected = [
    '\t{\n\t\ttest: /\\bpremium request allowance\\b/i,\n\t\treason: "rate_limit"\n\t}',
    '\t{\n\t\ttest: /\\btoken exchange failed\\b/i,\n\t\treason: "rate_limit"\n\t}',
  ].join(",\n");
  const before = errorsSource;
  errorsSource = errorsSource.replace(
    /(const PROVIDER_SPECIFIC_PATTERNS\s*=\s*\[)(\s*)/,
    (_, open, ws) => `${open}\n${injected},${ws}`
  );
  if (errorsSource === before) {
    console.warn("[hotfix] WARNING: Copilot patterns could not be inserted — PROVIDER_SPECIFIC_PATTERNS format may have changed in this version");
  } else {
    fs.writeFileSync(errorsFile, errorsSource, "utf8");
    console.log(`Patched OpenClaw errors bundle: ${errorsFile}`);
  }
}

// ── Patch 3: token-exchange throw → retryable failure ────────────────────────
// "Copilot token exchange failed: HTTP 404/403" is thrown by OpenClaw's Copilot
// auth layer before any chat completion request is made, so it never reaches
// classifyFailoverReason / PROVIDER_SPECIFIC_PATTERNS.  The fallback chain
// therefore stalls and the agent reports failure instead of degrading to ollama.
//
// Find the exact throw site and replace it with an IIFE that attaches
// .rateLimitFailure = true to the error before throwing.  OpenClaw's
// model-selection loop checks this flag to decide whether to try the next model.
//
// ≥ 2026.6.7: throw moved from pi-embedded-*.js to provider-auth-*.js.
// Search all bundles to stay robust against future moves.
const allDistBundles = fs.readdirSync(distDir).filter((n) => /\.js$/.test(n));
const tokenExchangeBundle = allDistBundles.find((n) => {
  const src = fs.readFileSync(path.join(distDir, n), "utf8");
  // Use "Copilot token exchange failed: HTTP" (includes the HTTP status) to avoid
  // false-positives from files that only reference the string in an .includes() check.
  return src.includes("Copilot token exchange failed: HTTP");
});

if (!tokenExchangeBundle) {
  console.warn("[hotfix] WARNING: 'Copilot token exchange failed: HTTP' not found in any bundle — skipping token-exchange retryable patch");
} else {
  const teBundlePath = path.join(distDir, tokenExchangeBundle);
  let teSource = fs.readFileSync(teBundlePath, "utf8");
  const teBefore = teSource;
  // Match: throw new Error("Copilot token exchange failed..." or `Copilot token exchange failed...`)
  teSource = teSource.replace(
    /throw new Error\((`[^`]*[Cc]opilot token exchange failed[^`]*`|"[^"]*[Cc]opilot token exchange failed[^"]*")\)/gi,
    (_, msg) => `(()=>{const _e=new Error(${msg});_e.rateLimitFailure=true;throw _e;})()`
  );
  if (teSource === teBefore) {
    console.warn("[hotfix] WARNING: token-exchange throw site not found — retryable patch skipped");
  } else {
    fs.writeFileSync(teBundlePath, teSource, "utf8");
    console.log(`Patched token-exchange retryable in: ${teBundlePath}`);
  }
}
