const fs = require("fs");
const path = require("path");

const distDir = "/app/dist";

// Find the bundle that contains the target function — the bundle was split across
// multiple pi-embedded-*.js files in newer OpenClaw releases, so search them all.
const allBundles = fs
  .readdirSync(distDir)
  .filter((name) => /^pi-embedded-.*\.js$/.test(name));

const target = allBundles.find((name) => {
  const src = fs.readFileSync(path.join(distDir, name), "utf8");
  return src.includes("resolveActiveErrorContext");
});

if (!target) {
  console.warn("[hotfix] WARNING: resolveActiveErrorContext not found in any pi-embedded bundle — skipping runtime patch");
  process.exit(0);
}

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

// ── Patch 1: fallback message guard ──────────────────────────────────────────
// Ensure stale assistant error messages are not shown when falling back to a
// different provider. Guards the error message path so that only messages from
// the current provider/model context are surfaced.
//
// ≥ 2026.5.7: upstream extracted the inline expression into a dedicated
// resolveAssistantFailoverErrorMessage() function — target that function.
// < 2026.5.7: the expression was inline as `const message = ...` — keep as
// fallback so this hotfix stays compatible with older base images.
const patchedFn = replaceOne(
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
if (!patchedFn) {
  replaceOne(
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

fs.writeFileSync(filePath, source, "utf8");
console.log(`Patched OpenClaw runtime bundle: ${filePath}`);

// ── Patch 2: errors bundle — add GitHub Copilot premium-allowance failover ──
// When Copilot exhausts the gpt-5.4 premium quota it returns an error like:
//   "You have exceeded your premium request allowance. We have automatically
//    switched you to GPT-4.1 which is included with your plan."
// classifyFailoverReason() doesn't recognise this, so OpenClaw never triggers
// the configured gpt-4.1 / ollama fallbacks.  Classify it as "rate_limit" so
// the fallback chain fires automatically.
// ── Patch 2: errors bundle — Copilot premium-allowance failover ──────────────
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
  const providerPatternsOpen = "const PROVIDER_SPECIFIC_PATTERNS = [\n";
  const copilotPremiumEntry = '\t{\n\t\ttest: /\\bpremium request allowance\\b/i,\n\t\treason: "rate_limit"\n\t},\n';
  errorsSource = errorsSource.replace(providerPatternsOpen, providerPatternsOpen + copilotPremiumEntry);
  fs.writeFileSync(errorsFile, errorsSource, "utf8");
  console.log(`Patched OpenClaw errors bundle: ${errorsFile}`);
}
