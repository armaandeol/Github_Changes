# Feature Flags: When to Gate, and How to Clean Up

*Original summary written for this demo corpus. Not a reproduction of any published standard.*

## When a Change Should Be Gated

A feature flag decouples deployment from release: the code ships dark and is turned on
separately, so exposure can be reversed in seconds without a redeploy. That property is
worth the added complexity in specific situations, and not otherwise.

Gate a change when:

- It alters a **security-relevant path** — authentication, session handling,
  authorization checks — where a regression is severe and detection may lag. A flag turns
  a rollback into a configuration change measured in seconds.
- It carries **meaningful uncertainty**: a rewritten query layer, a new algorithm, an
  integration with a dependency whose behavior under production load is unknown.
- It needs **progressive exposure** — a percentage rollout, an internal-first cohort, or
  a cohort defined by tenant or region.
- It is a **long-running migration** where old and new implementations must coexist, with
  the flag selecting between them so both stay exercised.
- Its **blast radius is broad** relative to how quickly a redeploy could mitigate it.

Do not gate trivial or self-contained changes. Every flag is a branch in production
behavior, and flags multiply combinatorially: five concurrent flags describe thirty-two
possible system states, most of which nobody has tested.

## Implementing Flags Well

Keep the flag check as close to the entry point of the new behavior as possible, so the
old path remains intact and genuinely reachable rather than partially executed. Default to
the safe value: if the flag service is unreachable, the system should fall back to the
existing behavior, never to the untested one.

Both branches must be tested. A flagged-off feature that has never been exercised in an
integration test is not "safely dark" — it is untested code that will be enabled by
someone under time pressure.

Make flags observable. Emit the resolved flag state with request telemetry so that when an
error rate rises, the cohort can be identified immediately. Without that, a percentage
rollout produces a confusing partial signal that is hard to attribute.

Log and audit flag changes as deployments in their own right — who flipped what, when.
A configuration change that alters production behavior deserves the same traceability as a
code deploy.

## Flag Hygiene and Cleanup

Release flags are temporary by definition, and stale flags are a real maintenance cost:
they accumulate dead branches, obscure the actual behavior, and eventually cause incidents
when someone removes the wrong side.

Give each flag an owner and an expected removal date at creation, and treat an overdue
flag as a tracked defect. Once a feature is fully rolled out and stable, removing the flag
is part of finishing the work — delete the branch, the flag definition, and the tests for
the retired path in one change. Distinguish short-lived release flags from long-lived
operational switches such as kill switches and licensing gates, which are intended to
persist and should be documented as such rather than swept up in cleanup.
