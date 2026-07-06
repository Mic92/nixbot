# Review: "TC: Lightweight Container Image for Cross-Platform Deployment"

## Summary

TC ships a container "image" that contains only the identifiers of an
application's direct dependencies. Instead of a fully built, platform-specific
environment, a lazy-builder on the deployment host resolves those identifiers
into concrete "uniform components" and overlays them into a container at deploy
time. Every package manager (apt, nix, pip, npm, conda, docker) is reduced to
two functions, version selection and environment selection, and a CDCL resolver
handles cross-manager dependencies on top of a central component registry.

The reported wins are 95% smaller images, 76-86% faster builds, 44-50% less
network traffic, and 40-60% faster deployment than Docker, Buildah and
Apptainer.

I think the direction is sound for interpreted-language apps that need to run on
many platforms, and I believe the measurements. What I don't buy is how much of
this is new versus a repackaging of what Nix and conda already do, and several
of the headline numbers only look that good because of how the baseline was set
up.

## Strengths

A few things stand out and should be said before the criticism.

The headline result is genuinely surprising: lazy-building a container from a
thin manifest ends up _faster_ than pulling a pre-built image (199.1 s to 12.7 s
on YOLO11, §Motivation). Beating a plain `docker pull` is not what I expected
going in, and if it holds up it is the most interesting claim in the paper.

The VS/ES abstraction is clean. Collapsing apt, nix, pip, npm, conda and docker
into just version selection plus environment selection, with a shared building
context carrying host facts across managers, is a tidy framing that makes
cross-manager dependencies (a pip package pulling apt libraries, ABI facts
propagating between them) expressible in one resolver. Even if the pieces exist
elsewhere, stating them this uniformly is useful.

The component-level sharing analysis (Table 1) is a real contribution, not just
a size claim. It shows a better sharing rate (21.6%) than file-level (14.6%) or
chunk-level (19.9%) approaches while producing far fewer objects (3302), and the
active-sharing variant pushes reuse to 46-69% on CPU/GPU servers. That is a
concrete argument for the granularity choice.

## Motivation

The paper justifies building its own machinery like this:

> If the lazy-builder were to directly reuse existing environment managers such
> as nix, pip or apt on the deployment platform, the build process would be
> slower than pulling a pre-built image, and the results could be incorrect or
> inconsistent due to the instability of upstream software registries.

Registry instability is largely a problem you opt into. Anyone deploying at
scale mirrors their registries internally (Artifactory, Nexus, private channels,
binary caches). Pin that mirror and the instability argument disappears, and a
pinned, warm registry is exactly what TC itself relies on.

The "reusing Nix would be slower" claim is asserted, not shown. In our own
experience Nix tends to beat the OCI model rather than lose to it: it shares
data at the store-path level instead of per layer, and it does not invalidate
everything downstream when an early build step changes, the way a Dockerfile
does. [nix-snapshotter](https://github.com/pdtpartners/nix-snapshotter) already
mounts store paths lazily as container content, which is the very thing the
paper claims existing managers cannot do fast enough.

## Missing baselines

Everything is compared against image builders (Docker, Buildah, Apptainer). The
systems that actually resemble TC are not. Bazel and Nix (`dockerTools`,
`nix2container`, or on-demand serving like Nixery) already build images, or skip
OCI entirely, from a single declarative description, and can materialise them
lazily. That is TC's own "declare once, build per platform" pitch, and it is not
evaluated against any of them. The two contributions the paper claims, unified
cross-manager resolution and deterministic selection, are mostly already there
in these tools.

## Evaluation setup

The 95% size number compares a manifest against a full image. TC is small only
because the ~1.8 GB of components live in a registry the host still has to pull.
The paper's own figures have the host fetching 188 MB of TC plus 1822 MB of
components, so the real end-to-end saving is about 65%.

The build-time advantage is pre-computation, not networking, as the authors note
(§5.1): the host only resolves and overlays, while conversion and compilation
happened earlier and centrally in the 1.6 TB registry, whereas Docker reruns apt
install, compilation and linking every build. That is the ~100 s gap in §5.4.
Fair design choice, but the work was moved to the central service, not removed;
a like-for-like comparison would give Docker a warm BuildKit layer cache.

Cross-platform portability (§5.2) is shown on a single app, YOLO11, not the full
nine-app suite used elsewhere.
