// -*- mode: Bluespec; -*-
// effect_runs rows of one build (two onPush effects, B after A, one check,
// one onEvent delivery) under build finish, reuse by another ref, build restart, effects
// restart, the work queue and crash recovery. "allowed" is a property of
// the ref reaching the build, so it is chosen per call.
//
// Rule: only maybe_run_effects creates pending rows, and enqueues their
// items in the same step. Restarts delete rows, never reset them.

module effects {

  type BStatus = BPending | BRunning | BSucceeded | BFailed | BCancelled
  type RStatus = None | Pending | Running | Succeeded | Failed | DepFailed | Skipped | Cancelled
  type QStatus = QPending | QRunning
  type Item = { id: int, row: str, st: QStatus }
  type Ref = Unset | Gated | Allowed   // builds.effects_branch/pr

  pure val A = "push.a"
  pure val B = "push.b"
  pure val C = "check.c"
  pure val D = "event.d"                // owner = delivery
  pure val ROWS = Set(A, B, C)
  pure val ALL = ROWS.union(Set(D))
  pure val PUSH = Set(A, B)
  pure def deps(r: str): Set[str] = if (r == B) Set(A) else Set()

  var build: BStatus
  var effectsStarted: bool         // builds.effects_started
  var ref: Ref
  var busy: bool                   // Orchestrator.cancel_events
  var rows: str -> RStatus         // None = no row
  var items: Set[Item]             // work_queue kind='effect'
  var tasks: Set[str]              // live asyncio task past ClaimEffect
  var nextId: int
  var crashed: bool

  action init = all {
    build' = BPending, effectsStarted' = false, ref' = Unset, busy' = false,
    rows' = ALL.mapBy(_ => None), items' = Set(), tasks' = Set(), nextId' = 1, crashed' = false,
  }

  pure def live(s: RStatus): bool = s == Pending or s == Running

  // EnqueueEffectItems dedup
  pure def enqueue(q: Set[Item], id: int, names: Set[str]): Set[Item] =
    names.fold(q, (acc, n) =>
      if (acc.exists(i => i.row == n and i.st == QPending)) acc
      else acc.union(Set({ id: acc.fold(id, (m, i) => if (i.id >= m) i.id + 1 else m), row: n, st: QPending })))

  // Returns (rows, items, effectsStarted). `names` have no row.
  pure def maybeRunEffects(r: str -> RStatus, q: Set[Item], started: bool, allowed: bool, id: int,
                           names: Set[str]): (str -> RStatus, Set[Item], bool) = {
    if (allowed and started) (r, q, started) else {
      val checks = names.exclude(PUSH)
      val push = names.intersect(PUSH)
      val r1 = names.fold(r, (acc, n) => acc.set(n, if (allowed or checks.contains(n)) Pending else Skipped))
      (r1, enqueue(q, id, if (allowed) names else checks), started or allowed)
    }
  }

  action startBuild = all {
    not(crashed), build == BPending, not(busy),
    build' = BRunning, busy' = true,
    effectsStarted' = effectsStarted, ref' = ref, rows' = rows, items' = items, tasks' = tasks,
    nextId' = nextId, crashed' = crashed,
  }

  // RecordEffectsRef: a gated ref never revokes an allowed one.
  pure def gate(ref: Ref, allowed: bool): bool = allowed or ref == Allowed
  pure def record(ref: Ref, allowed: bool): Ref = if (allowed) Allowed else if (ref == Unset) Gated else ref

  // `allowed`: whichever ref reaches the rebuilt build, its own on a restart.
  action finishBuild(ok: bool, allowed: bool): bool = all {
    not(crashed), build == BRunning, busy,
    if (ok) {
      val a = gate(ref, allowed)
      val res = maybeRunEffects(rows, items, effectsStarted, a, nextId, ROWS)
      all { build' = BSucceeded, rows' = res._1, items' = res._2, effectsStarted' = res._3, ref' = record(ref, a) }
    } else all {
      build' = BFailed, rows' = rows, items' = items, effectsStarted' = effectsStarted, ref' = ref,
    },
    busy' = false, tasks' = tasks, nextId' = nextId + 4, crashed' = crashed,
  }

  action cancelBuild = all {
    not(crashed), (build == BPending or build == BRunning),
    build' = BCancelled, busy' = false,
    rows' = rows, effectsStarted' = effectsStarted, ref' = ref, items' = items, tasks' = tasks,
    nextId' = nextId, crashed' = crashed,
  }

  action reuseBuild(allowed: bool): bool = all {
    not(crashed), not(busy), (build == BSucceeded or build == BFailed),
    if (build == BSucceeded and not(effectsStarted)) {
      // an allowed ref drops what a gated pass left and rediscovers
      val a = gate(ref, allowed)
      val r0 = if (a) ALL.mapBy(n => if (n == D) rows.get(D) else None) else rows
      val q0 = if (a) items.filter(i => i.row == D or (i.st == QRunning and not(tasks.contains(i.row)))) else items
      val res = maybeRunEffects(r0, q0, false, a, nextId, ROWS.filter(n => r0.get(n) == None))
      all { rows' = res._1, items' = res._2, effectsStarted' = res._3, ref' = record(ref, a),
            tasks' = if (a) tasks.intersect(Set(D)) else tasks }
    } else all { rows' = rows, items' = items, effectsStarted' = effectsStarted, ref' = ref, tasks' = tasks },
    build' = build, busy' = busy, nextId' = nextId + 4, crashed' = crashed,
  }

  // cancel_running_effects awaits the tasks. Claimed items without a task
  // yet stay and find no row.
  action restartBuild = all {
    not(crashed), not(busy), build != BPending, build != BRunning,
    build' = BPending,
    rows' = ALL.mapBy(n => if (n != D) None else if (rows.get(D) == Pending) Cancelled else rows.get(D)),
    items' = items.filter(i => i.row == D or (i.st == QRunning and not(tasks.contains(i.row)))),
    tasks' = tasks.intersect(Set(D)),
    effectsStarted' = false,
    ref' = ref, busy' = busy, nextId' = nextId, crashed' = crashed,
  }

  // `only` pulls in dependents that are dependency_failed or pending.
  action restartEffects(only: Set[str]): bool = all {
    not(crashed), not(busy), build == BSucceeded, ref != Unset,
    {
      val names = if (only == Set()) ROWS
                  else PUSH.filter(n => only.contains(n) or
                         (deps(n).intersect(only) != Set() and (rows.get(n) == DepFailed or rows.get(n) == Pending)))
      val r1 = ALL.mapBy(n => if (names.contains(n)) None else rows.get(n))
      val q1 = items.filter(i => not(names.contains(i.row)) or (i.st == QRunning and not(tasks.contains(i.row))))
      val res = maybeRunEffects(r1, q1, false, ref == Allowed, nextId, names)
      all { rows' = res._1, items' = res._2, effectsStarted' = res._3, tasks' = tasks.exclude(names) }
    },
    build' = build, ref' = ref, busy' = false, nextId' = nextId + 4, crashed' = crashed,
  }

  // UpsertEventEffect on a settled build: resets unless running.
  action deliver = all {
    not(crashed), not(busy), (build == BSucceeded or build == BFailed),
    rows.get(D) != Running,
    rows' = rows.set(D, Pending), items' = enqueue(items, nextId, Set(D)),
    build' = build, effectsStarted' = effectsStarted, ref' = ref, busy' = busy, tasks' = tasks,
    nextId' = nextId + 1, crashed' = crashed,
  }

  // restart_event_effect: cancel the task, ResetEventEffect, enqueue.
  action restartEvent = all {
    not(crashed), rows.get(D) != None,
    rows' = rows.set(D, Pending), tasks' = tasks.exclude(Set(D)),
    items' = enqueue(items.filter(i => not(i.row == D and i.st == QRunning and tasks.contains(D))), nextId, Set(D)),
    build' = build, effectsStarted' = effectsStarted, ref' = ref, busy' = busy,
    nextId' = nextId + 1, crashed' = crashed,
  }

  // CancelEffects: stop without re-running.
  action cancelEffect(n: str): bool = all {
    not(crashed), live(rows.get(n)),
    rows' = rows.set(n, Cancelled), tasks' = tasks.exclude(Set(n)),
    items' = items.filter(i => i.row != n or (i.st == QRunning and not(tasks.contains(n)))),
    build' = build, effectsStarted' = effectsStarted, ref' = ref, busy' = busy,
    nextId' = nextId, crashed' = crashed,
  }

  // ClaimNextWorkItem
  action claim(id: int): bool = all {
    not(crashed),
    items.exists(i => i.id == id and i.st == QPending and
      not(items.exists(o => o.row == i.row and (o.st == QRunning or (o.st == QPending and o.id < i.id)))) and
      deps(i.row).forall(d => not(live(rows.get(d))))),
    items' = items.map(i => if (i.id == id) { ...i, st: QRunning } else i),
    build' = build, effectsStarted' = effectsStarted, ref' = ref, busy' = busy, rows' = rows,
    tasks' = tasks, nextId' = nextId, crashed' = crashed,
  }

  def claimed(n: str): bool = items.exists(i => i.row == n and i.st == QRunning) and not(tasks.contains(n))
  pure def dropRunning(q: Set[Item], n: str): Set[Item] = q.filter(i => not(i.row == n and i.st == QRunning))

  action itemStart(n: str): bool = all {
    not(crashed), claimed(n),
    if (rows.get(n) != Pending)
      all { rows' = rows, items' = dropRunning(items, n), tasks' = tasks }
    else if (deps(n).exists(d => rows.get(d) != Succeeded))
      all { rows' = rows.set(n, DepFailed), items' = dropRunning(items, n), tasks' = tasks }
    else
      all { rows' = rows.set(n, Running), items' = items, tasks' = tasks.union(Set(n)) },
    build' = build, effectsStarted' = effectsStarted, ref' = ref, busy' = busy, nextId' = nextId, crashed' = crashed,
  }

  // FinishEffect only moves a live row.
  action itemFinish(n: str, ok: bool): bool = all {
    not(crashed), tasks.contains(n),
    rows' = if (live(rows.get(n))) rows.set(n, if (ok) Succeeded else Failed) else rows,
    items' = dropRunning(items, n),
    tasks' = tasks.exclude(Set(n)),
    build' = build, effectsStarted' = effectsStarted, ref' = ref, busy' = busy, nextId' = nextId, crashed' = crashed,
  }

  action crash = all {
    not(crashed),
    crashed' = true, tasks' = Set(), busy' = false,
    build' = build, effectsStarted' = effectsStarted, ref' = ref, rows' = rows, items' = items, nextId' = nextId,
  }

  // SettleInterruptedWork, FailInterruptedEffects, resume live builds.
  action recover = all {
    crashed,
    items' = items.filter(i => not(i.st == QRunning and items.exists(p => p.row == i.row and p.st == QPending)))
                  .map(i => if (i.st == QRunning) { ...i, st: QPending } else i),
    rows' = ALL.mapBy(n => if (rows.get(n) == Running) Failed else rows.get(n)),
    busy' = (build == BRunning),
    build' = build, effectsStarted' = effectsStarted, ref' = ref, tasks' = tasks, nextId' = nextId, crashed' = false,
  }

  action step = any {
    startBuild,
    nondet ok = oneOf(Set(true, false))
    nondet allowed = oneOf(Set(true, false))
    finishBuild(ok, allowed),
    cancelBuild,
    nondet allowed = oneOf(Set(true, false))
    reuseBuild(allowed),
    restartBuild,
    nondet only = oneOf(Set(Set(), Set(A), Set(B)))
    restartEffects(only),
    deliver,
    restartEvent,
    nondet n = oneOf(ALL)
    cancelEffect(n),
    nondet id = oneOf(1.to(nextId + 4))
    claim(id),
    nondet n = oneOf(ALL)
    itemStart(n),
    nondet n = oneOf(ALL)
    nondet ok = oneOf(Set(true, false))
    itemFinish(n, ok),
    crash,
    recover,
  }

  def quiet: bool = not(crashed) and not(busy) and items == Set() and tasks == Set() and
                    build != BPending and build != BRunning

  val settled = quiet implies ALL.forall(n => not(live(rows.get(n))))

  val pendingQueued = not(crashed) implies
    ALL.forall(n => rows.get(n) == Pending implies items.exists(i => i.row == n))

  val runningOwned = ALL.forall(n => (rows.get(n) == Running) implies (tasks.contains(n) or crashed)) and
                     ALL.forall(n => tasks.contains(n) implies rows.get(n) == Running)

  val singleRunner = ALL.forall(n => items.filter(i => i.row == n and i.st == QRunning).size() <= 1)

  val gated = PUSH.forall(n => (live(rows.get(n)) or tasks.contains(n)) implies (effectsStarted and ref == Allowed))

  val startedAllowed = effectsStarted implies ref == Allowed

  val skippedGated = PUSH.forall(n => rows.get(n) == Skipped implies not(effectsStarted))


  val depOrder = PUSH.forall(n => tasks.contains(n) implies deps(n).forall(d => rows.get(d) == Succeeded))

  val safety = settled and pendingQueued and runningOwned and singleRunner and gated and
               startedAllowed and skippedGated and depOrder
}
