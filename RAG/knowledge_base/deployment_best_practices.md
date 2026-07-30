# Deployment Practices: Staged Rollouts, Canaries, and Rollback

*Original summary written for this demo corpus. Not a reproduction of any published standard.*

## Staged Rollouts

Deploying a change to the entire fleet at once converts every defect into a total outage.
Staged rollout replaces that with a progression: an internal or test cohort first, then a
small percentage of production traffic, then successively larger rings, with a bake period
at each stage long enough for slow-burning failures to appear. A regression caught in the
first ring affects a small fraction of users and is attributable to exactly one change.

Ring size should reflect risk, not habit. A change touching authentication, permissions,
or data-access paths warrants a smaller first ring and a longer bake than a copy change.
Deploy one meaningful change per rollout where possible; when several changes ride
together, a bad signal cannot be attributed without unwinding all of them.

Avoid deploying risky changes immediately before periods of reduced staffing. The cost of
a bad deploy is dominated by time-to-detection and time-to-mitigation, both of which
degrade when nobody is available to respond.

## Canary Deployments

A canary sends a small slice of live traffic to the new version while the old version
continues serving the rest, and compares the two directly. The comparison is what makes a
canary more than a small rollout: baseline and candidate experience the same traffic mix
at the same time, so seasonal and load effects cancel out.

Define the promotion criteria before deploying, and make them automatic. Useful signals:
error rate, latency at high percentiles rather than the mean, saturation of CPU/memory/
connection pools, and a small number of business-level indicators such as login success
rate or checkout completion. For an authentication change, login success rate and
authorization-denial rate are the signals that matter most, and both should be wired up
before the canary starts rather than inspected manually afterward.

A canary needs a defined duration. Promoting after two minutes only proves the process
starts; defects in cache warm-up, connection recycling, or scheduled jobs surface later.
Automate the rollback trigger so mitigation does not wait on human judgment.

## Rollback Planning

Every deployment needs a rollback path identified before it starts, and the honest question
is whether the change is actually reversible. Code is usually reversible; a data migration
that drops a column or rewrites rows in place is not. Where a change is not reversible by
redeploying the previous artifact, the rollout plan must sequence it so that the
irreversible step lands separately from the behavior change and after the behavior change
has proven stable.

Rollback must be routine and fast — practiced, scripted, and exercised, not improvised
during an incident. Record the previous known-good version as part of the deploy so it is
unambiguous under pressure.

Instrumentation is what makes any of this work. Before the deploy, know which dashboard
and which alert will indicate this specific change failing, and confirm those signals
exist. A rollout without a defined failure signal is untestable in production, and the
correct response is to add the signal first, not to deploy and watch.
