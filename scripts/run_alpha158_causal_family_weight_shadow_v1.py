#!/usr/bin/env python3
"""Frozen prospective paper account for causal seven-family Alpha158 weights.

The account consumes the already committed daily market snapshot from the
existing Alpha158 forward experiment.  It owns a separate empty ledger and
never creates broker orders.  Family models are rebuilt once from the exact
last historical OOS-fold training boundary, parity checked, then archived.
"""
from __future__ import annotations

import argparse
import fcntl
import gc
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu import ABuAlphaForwardShadow as shadow  # noqa: E402
from abupy.AlphaBu.ABuAlpha158Canonical import (  # noqa: E402
    ALPHA158_CANONICAL_FAMILIES, Alpha158CanonicalFeatureEngine,
)
from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    ALPHA158_LITE_FEATURES, Alpha158LiteFeatureEngine,
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.run_alpha158_forward_shadow_v1 import apply_spot_status  # noqa: E402
from scripts.validate_alpha158_canonical_family_v1 import (  # noqa: E402
    fit_candidate,
)
from scripts.validate_alpha158_residual_overheat_v1 import (  # noqa: E402
    ALL_FEATURES, ResidualOverheatFeatures, fit_extended_ridge,
)


DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_causal_family_weight_forward_v1.json"
ACCOUNT = "causal_family_weight"
KEYS = ("signal_asof", "symbol", "column")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_config(path=DEFAULT_CONFIG):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if (config["real_orders_allowed"] or config["broker_connected"] or
            config["automatic_admission"] or
            config["historical_orders_backfilled"] or
            config["missed_session_backfill"]):
        raise ValueError("forward shadow isolation contract changed")
    if config["ranking_exits"]:
        raise ValueError("candidate must retain event-exit-only execution")
    if config["target_positions"] != 10 or not np.isclose(
            config["portfolio_open_risk_fraction"], .02):
        raise ValueError("portfolio construction differs from frozen history")
    if (config["label_embargo_sessions"] != 21 or
            config["weight_lookback_sessions"] != 252 or
            config["minimum_weight_history_sessions"] != 126 or
            not np.isclose(config["uniform_shrinkage"], .5)):
        raise ValueError("causal family weight protocol changed")
    return config


def _family_files(config):
    root = Path(config["historical_family_root"])
    return {family: root / family / "oos_predictions.csv.gz"
            for family in config["families"]}


def weights_asof(ic_history, panel_dates, current_day, config):
    """Return weights available at close, excluding the latest 21 sessions."""
    families = list(config["families"])
    equal = pd.Series(1.0 / len(families), index=families, dtype=float)
    eligible_position = int(current_day) - int(config["label_embargo_sessions"])
    if eligible_position < 0:
        return equal, None, 0
    eligible_date = int(panel_dates[eligible_position])
    work = ic_history[
        ic_history.signal_asof.astype(int).le(eligible_date) &
        ic_history.family.isin(families)].copy()
    pivot = work.pivot_table(
        index="signal_asof", columns="family", values="rank_ic",
        aggfunc="last").sort_index().reindex(columns=families)
    pivot = pivot.tail(int(config["weight_lookback_sessions"]))
    counts = pivot.notna().sum()
    if pivot.empty or (counts < int(
            config["minimum_weight_history_sessions"])).any():
        return equal, eligible_date, int(len(pivot))
    raw = pivot.mean().clip(lower=float(config["negative_family_weight"]))
    if not np.isfinite(raw).all() or float(raw.sum()) <= 0:
        return equal, eligible_date, int(len(pivot))
    raw = raw / raw.sum()
    shrink = float(config["uniform_shrinkage"])
    result = shrink * equal + (1.0 - shrink) * raw
    return result / result.sum(), eligible_date, int(len(pivot))


def combine_family_scores(frames, weights):
    base = None
    for family, frame in frames.items():
        work = frame[[*KEYS, "candidate_score"]].sort_values(
            list(KEYS), kind="mergesort").reset_index(drop=True)
        work["family_rank"] = work.groupby(
            "signal_asof", sort=False).candidate_score.rank(
                method="average", pct=True).to_numpy(dtype=float) - .5
        work["family"] = family
        if base is None:
            base = work[[*KEYS]].copy()
            base["alpha_score"] = 0.0
        elif not base[list(KEYS)].equals(work[list(KEYS)]):
            raise ValueError("prospective family score keys differ")
        base["alpha_score"] += float(weights[family]) * work.family_rank
        frames[family] = work
    base = base.sort_values(
        ["alpha_score", "symbol"], ascending=[False, True],
        kind="mergesort").reset_index(drop=True)
    base["daily_rank"] = np.arange(1, len(base) + 1)
    detail = pd.concat(frames.values(), ignore_index=True)
    return base, detail


def _training_days(panel, source, manifest, stride):
    positions = np.arange(len(panel.dates))
    days = positions[
        (panel.dates >= int(manifest["train_start"])) &
        (panel.dates <= int(manifest["train_end"])) &
        (positions >= int(source.minimum_history_sessions))]
    return days[::int(stride)]


def _engine(panel, source, family):
    if family == "residual_overheat":
        return ResidualOverheatFeatures(panel, source), ALL_FEATURES
    engine = Alpha158CanonicalFeatureEngine(panel, source, family)
    return engine, (*ALPHA158_LITE_FEATURES, *engine.feature_names)


def fit_frozen_models(panel, source, config):
    """Rebuild each exact final-fold model and verify its historical scores."""
    models, manifests = {}, {}
    for family, prediction_path in _family_files(config).items():
        family_root = prediction_path.parent
        folds = json.loads((family_root / "fold_manifests.json").read_text())
        manifest = folds[-1]
        engine, features = _engine(panel, source, family)
        days = _training_days(
            panel, source, manifest,
            config["family_model_training_stride_sessions"])
        frames = []
        for count, day in enumerate(days):
            frames.append(engine.snapshot(int(day)))
            if count % 80 == 0:
                print("TRAIN {} {}/{}".format(
                    family, count + 1, len(days)), flush=True)
        training = pd.concat(frames, ignore_index=True)
        if family == "residual_overheat":
            model, model_manifest = fit_extended_ridge(training, source)
        else:
            model, model_manifest = fit_candidate(training, source, features)
        expected = pd.read_csv(prediction_path, dtype={"symbol": str})
        parity_date = int(expected.signal_asof.max())
        day = int(np.flatnonzero(panel.dates == parity_date)[0])
        snapshot = engine.snapshot(day, include_labels=False) \
            if family != "residual_overheat" else engine.snapshot(day)
        actual = pd.DataFrame({
            "symbol": snapshot.symbol,
            "candidate_score_actual": model.predict(snapshot[list(features)]),
        })
        check = expected[expected.signal_asof.eq(parity_date)][
            ["symbol", "candidate_score"]].merge(
                actual, on="symbol", how="inner", validate="one_to_one")
        error = float(np.max(np.abs(
            check.candidate_score - check.candidate_score_actual)))
        if len(check) != len(expected[expected.signal_asof.eq(parity_date)]) or \
                error > float(config[
                    "family_model_parity_absolute_tolerance"]):
            raise ValueError("final-fold model parity failed: {} {}".format(
                family, error))
        model_manifest.update({
            "family": family,
            "historical_fold": int(manifest["fold"]),
            "train_start": int(manifest["train_start"]),
            "train_end": int(manifest["train_end"]),
            "train_label_end": int(manifest["train_label_end"]),
            "training_stride_sessions": int(
                config["family_model_training_stride_sessions"]),
            "parity_date": parity_date,
            "parity_rows": int(len(check)),
            "maximum_absolute_parity_error": error,
            "update_policy": config["family_model_policy"],
        })
        models[family] = model
        manifests[family] = model_manifest
        del training, frames, engine, snapshot, expected, actual, check
        gc.collect()
    return models, manifests


def score_panel(panel, source, models, weights):
    day = len(panel.dates) - 1
    frames = {}
    for family, model in models.items():
        engine, features = _engine(panel, source, family)
        snapshot = engine.snapshot(day, include_labels=False) \
            if family != "residual_overheat" else engine.snapshot(day)
        frame = snapshot[[*KEYS]].copy()
        frame["candidate_score"] = model.predict(snapshot[list(features)])
        frames[family] = frame
    return combine_family_scores(frames, weights)


def _seed_ic(config):
    path = Path(config["historical_experiment_root"]) / \
        "family_daily_rank_ic.csv"
    frame = pd.read_csv(path)
    if set(frame.family) != set(config["families"]):
        raise ValueError("historical family IC seed differs")
    return frame.sort_values(["signal_asof", "family"]).reset_index(drop=True)


def _history(paths):
    history = [pd.read_csv(paths[0] / "family_daily_rank_ic.csv")]
    for directory in paths[1:]:
        path = directory / "matured_family_ic.csv"
        if path.exists() and path.stat().st_size:
            frame = pd.read_csv(path)
            if len(frame):
                history.append(frame)
    return pd.concat(history, ignore_index=True).drop_duplicates(
        ["signal_asof", "family"], keep="last")


def matured_family_ic(paths, panel, source, config):
    day = len(panel.dates) - 1
    position = day - int(config["label_embargo_sessions"])
    columns = ["signal_asof", "rank_ic", "family"]
    if position < 0:
        return pd.DataFrame(columns=columns)
    signal_date = int(panel.dates[position])
    source_dir = next((path for path in paths[1:]
                       if int(path.name) == signal_date), None)
    if source_dir is None:
        return pd.DataFrame(columns=columns)
    family_scores = pd.read_csv(
        source_dir / "family_scores.csv.gz", dtype={"symbol": str})
    labeled = Alpha158LiteFeatureEngine(panel, source).snapshot(
        position, include_labels=True)[["symbol", "excess_return_20d"]]
    rows = []
    for family in config["families"]:
        frame = family_scores[family_scores.family.eq(family)].merge(
            labeled, on="symbol", how="inner", validate="one_to_one")
        frame = frame.dropna(subset=["candidate_score", "excess_return_20d"])
        if len(frame) < 20:
            continue
        value = frame.candidate_score.corr(
            frame.excess_return_20d, method="spearman")
        if np.isfinite(value):
            rows.append({"signal_asof": signal_date,
                         "rank_ic": float(value), "family": family})
    return pd.DataFrame(rows, columns=columns)


def _candidate_panel(base_root, research_dir, end_date):
    panel = SelectionPanelV2.from_research_data(
        Path(base_root) / "data/signal", research_dir,
        start_date=20110101, end_date=int(end_date))
    apply_spot_status(panel, Path(base_root) / "collector")
    actions = pd.read_csv(
        Path(base_root) / "data/research/corporate_actions.csv",
        dtype={"symbol": str})
    shadow.forward_actions(panel, actions, after_date=20260930)
    return panel


def _base_latest(base_root):
    paths, digest = shadow.verify_chain(base_root)
    state = shadow.load_pickle(paths[-1] / "state.pkl.gz")
    return paths, digest, state


def verify(root):
    root = Path(root)
    paths, digest = shadow.verify_chain(root)
    registration = json.loads((root / "genesis/registration.json").read_text())
    shadow.verify_files(root / "runtime", registration["runtime_files"])
    base_root = Path(registration["baseline_forward_root"])
    if sha256(base_root / "genesis/commit.json") != \
            registration["baseline_genesis_sha256"]:
        raise ValueError("registered baseline genesis changed")
    config = registration["protocol"]
    load_config(root / "runtime/configs/selection/alpha158_causal_family_weight_forward_v1.json")
    if config["real_orders_allowed"] or config["broker_connected"]:
        raise ValueError("paper-only registration changed")
    return paths, digest, registration


def initialize(root, config_path):
    config = load_config(config_path)
    root = Path(root)
    if root.exists():
        raise FileExistsError("shadow already exists; never reset")
    base_root = Path(config["baseline_forward_root"])
    _, base_digest, base_state = _base_latest(base_root)
    if int(base_state["sessions"]) != 0:
        raise ValueError("paired shadow must start before baseline first session")
    cutoff = int(base_state["last_processed_date"])
    staging = root.parent / ("." + root.name + ".init-" + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    runtime = staging / "runtime"
    runtime.mkdir()
    for name in ("abupy", "scripts", "configs/selection"):
        shutil.copytree(ROOT / name, runtime / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    runtime_config = runtime / "configs/selection" / Path(config_path).name
    base_configs = Path(config["historical_experiment_root"]) / "frozen_inputs"
    for name in ("alpha158_lite_v1.json",
                 "alpha158_lite_low_turnover_v3.json", "risk_v1.json"):
        shutil.copy2(base_configs / name, runtime / "configs/selection" / name)
    source = load_alpha158_lite_config(
        runtime / "configs/selection/alpha158_lite_v1.json")
    policy = load_alpha158_lite_low_turnover_config(
        runtime / "configs/selection/alpha158_lite_low_turnover_v3.json")
    risk = load_risk_config(runtime / "configs/selection/risk_v1.json")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("frozen source and policy mismatch")
    if not np.isclose(risk.portfolio_open_risk_fraction,
                      config["portfolio_open_risk_fraction"]):
        raise ValueError("frozen risk budget mismatch")
    genesis = staging / ".genesis"
    genesis.mkdir()
    registration = {
        "registered_at": shadow.now_shanghai().isoformat(),
        "strategy_id": config["strategy_id"],
        "protocol": config,
        "baseline_forward_root": str(base_root),
        "baseline_genesis_sha256": sha256(base_root / "genesis/commit.json"),
        "baseline_chain_at_registration": base_digest,
        "historical_cutoff": cutoff,
        "historical_results_are_not_forward_evidence": True,
        "runtime_files": shadow.file_manifest(runtime),
        "family_prediction_hashes": {
            family: sha256(path) for family, path in _family_files(config).items()
        },
        "automatic_admission": False,
        "broker_connected": False,
        "real_orders_allowed": False,
        "account_isolation": "independent_cash_orders_positions_and_fills",
    }
    atomic_json(genesis / "registration.json", registration)
    print("LOAD FROZEN SHENZHEN TRAINING PANEL", flush=True)
    panel = _candidate_panel(
        base_root, Path(config["historical_research_dir"]), cutoff)
    if int(panel.dates[-1]) != cutoff:
        raise ValueError("candidate panel cutoff differs from baseline")
    print("FIT SEVEN FINAL-FOLD MODELS ONCE", flush=True)
    models, model_manifests = fit_frozen_models(panel, source, config)
    protocol = dict(
        config,
        initial_cash_per_account=float(config["initial_cash"]),
        minimum_candidate_rows=1500,
        minimum_entry_date_clusters_per_arm=int(
            config["minimum_entry_date_clusters"]),
    )
    state = shadow.new_state(
        panel, source, policy, risk, protocol,
        registration["registered_at"], account_specs={
            ACCOUNT: {"ranking_exits": False, "risk": risk},
        })
    seed = _seed_ic(config)
    weights, eligible, observations = weights_asof(
        seed, panel.dates, len(panel.dates) - 1, config)
    atomic_json(genesis / "model_manifest.json", model_manifests)
    atomic_json(genesis / "weight_seed_manifest.json", {
        "rows": int(len(seed)), "latest_signal": int(seed.signal_asof.max()),
        "eligible_through_at_cutoff": eligible,
        "rolling_observations": observations,
        "weights": {key: float(value) for key, value in weights.items()},
        "historical_seed_only": True,
    })
    seed.to_csv(genesis / "family_daily_rank_ic.csv", index=False)
    shadow.dump_pickle(genesis / "models.pkl.gz", models)
    shadow.dump_pickle(genesis / "panel.pkl.gz", panel)
    shadow.dump_pickle(genesis / "state.pkl.gz", shadow.checkpoint_state(state))
    shadow.export_accounts(genesis, state)
    shadow.commit_directory(genesis, staging / "genesis", "GENESIS")
    os.rename(staging, root)
    result = strategy_summary(root, state, weights, eligible, observations)
    atomic_json(root / "summary.json", result)
    return result


def strategy_summary(root, state, weights=None, eligible=None,
                     observations=None):
    base = shadow.summary(state)
    if weights is None:
        paths = shadow.verify_chain(root)[0]
        history = _history(paths)
        panel = shadow.last_panel(paths)
        config = json.loads((root / "genesis/registration.json").read_text())[
            "protocol"]
        weights, eligible, observations = weights_asof(
            history, panel.dates, len(panel.dates) - 1, config)
    base.update({
        "strategy_id": "alpha158_causal_family_rank_event_exit_only_v1",
        "current_family_weights": {
            key: float(value) for key, value in weights.items()},
        "weight_ic_eligible_through": eligible,
        "weight_rolling_observations": observations,
        "baseline_forward_root": str(json.loads(
            (Path(root) / "genesis/registration.json").read_text())[
                "baseline_forward_root"]),
        "independent_ledger": True,
    })
    return base


def run_session(root):
    root = Path(root)
    paths, previous, registration = verify(root)
    config = registration["protocol"]
    _, base_digest, base_state = _base_latest(
        Path(registration["baseline_forward_root"]))
    state = shadow.load_pickle(paths[-1] / "state.pkl.gz")
    base_date = int(base_state["last_processed_date"])
    if base_date == int(state["last_processed_date"]):
        return {"status": "WAITING_FOR_BASELINE_SESSION",
                "summary": strategy_summary(root, state)}
    panel = _candidate_panel(
        Path(registration["baseline_forward_root"]),
        Path(config["historical_research_dir"]), base_date)
    prior = shadow.last_panel(paths)
    shadow.assert_append_only(prior, panel)
    matured = matured_family_ic(paths, panel, state["source"], config)
    history = _history(paths)
    if len(matured):
        history = pd.concat([history, matured], ignore_index=True)
    weights, eligible, observations = weights_asof(
        history, panel.dates, len(panel.dates) - 1, config)
    models = shadow.load_pickle(root / "genesis/models.pkl.gz")
    scores, family_scores = score_panel(
        panel, state["source"], models, weights)
    shadow.step_accounts(state, panel, scores)
    stage = root / (".session-" + uuid.uuid4().hex)
    stage.mkdir()
    shadow.dump_pickle(stage / "panel_delta.pkl.gz",
                       shadow.panel_delta(prior, panel))
    shadow.dump_pickle(stage / "state.pkl.gz", shadow.checkpoint_state(state))
    scores.to_csv(stage / "features_scores.csv.gz", index=False,
                  float_format="%.17g")
    family_scores.to_csv(stage / "family_scores.csv.gz", index=False,
                         float_format="%.17g")
    matured.to_csv(stage / "matured_family_ic.csv", index=False)
    atomic_json(stage / "family_weights.json", {
        "trade_date": base_date,
        "eligible_ic_through": eligible,
        "rolling_observations": observations,
        "weights": {key: float(value) for key, value in weights.items()},
        "base_chain_commit": base_digest,
    })
    shadow.export_accounts(stage, state)
    shadow.commit_directory(
        stage, root / "sessions" / str(base_date), previous)
    result = strategy_summary(root, state, weights, eligible, observations)
    atomic_json(root / "summary.json", result)
    return {"status": "SESSION_COMMITTED", "summary": result}


def audit(root):
    root = Path(root)
    paths, _, registration = verify(root)
    config = registration["protocol"]
    state = shadow.load_pickle(paths[0] / "state.pkl.gz")
    models = shadow.load_pickle(paths[0] / "models.pkl.gz")
    panels = shadow.replay_panels(paths)
    next(panels)
    history = pd.read_csv(paths[0] / "family_daily_rank_ic.csv")
    for index, directory in enumerate(paths[1:], 1):
        panel = next(panels)
        matured = matured_family_ic(
            paths[:index], panel, state["source"], config)
        if len(matured):
            history = pd.concat([history, matured], ignore_index=True)
        weights, _, _ = weights_asof(
            history, panel.dates, len(panel.dates) - 1, config)
        scores, family_scores = score_panel(
            panel, state["source"], models, weights)
        saved = pd.read_csv(
            directory / "features_scores.csv.gz", dtype={"symbol": str})
        pd.testing.assert_frame_equal(
            scores, saved, check_dtype=False, rtol=1e-9, atol=1e-10)
        saved_family = pd.read_csv(
            directory / "family_scores.csv.gz", dtype={"symbol": str})
        pd.testing.assert_frame_equal(
            family_scores, saved_family, check_dtype=False,
            rtol=1e-9, atol=1e-10)
        shadow.step_accounts(state, panel, scores)
        expected = json.loads((directory / "summary.json").read_text())
        if shadow.summary(state) != expected:
            raise AssertionError("prospective ledger replay mismatch")
    return {"status": "AUDIT_PASSED", "sessions": len(paths) - 1,
            "family_predictions_replayed": True,
            "weights_replayed": True, "ledger_replayed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "run", "status", "audit"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    root = Path(args.root or config["shadow_root"]).resolve()
    if args.command == "init":
        result = initialize(root, args.config.resolve())
    else:
        verify(root)
        frozen = root / "runtime/scripts" / Path(__file__).name
        if Path(__file__).resolve() != frozen:
            os.execv(sys.executable, [sys.executable, "-B", str(frozen),
                                     args.command, "--root", str(root),
                                     "--config", str(root / "runtime/configs/selection" /
                                                     args.config.name)])
        with (root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "run":
                result = run_session(root)
            elif args.command == "audit":
                result = audit(root)
            else:
                paths = shadow.verify_chain(root)[0]
                result = strategy_summary(
                    root, shadow.load_pickle(paths[-1] / "state.pkl.gz"))
    print(json.dumps(result, ensure_ascii=False, indent=2,
                     sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
