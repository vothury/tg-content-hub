"""Одноразовая починка: переписывает блок создания TargetChannel в sources_sync.py
целиком, независимо от того, как именно склеились строки при ручной правке."""
from pathlib import Path

p = Path("app/services/sources_sync.py")
s = p.read_text(encoding="utf-8")
start = s.index('session.add(TargetChannel(username=cfg["username"]')
end = s.index("style_profile_id=style_id))", start) + len("style_profile_id=style_id))")
new = '''session.add(TargetChannel(username=cfg["username"], title=cfg["title"], description=cfg["description"],
                                          daily_limit=cfg["daily_limit"] or 6, min_interval_min=cfg["min_interval_min"] or 60,
                                          quiet_hours=cfg["quiet_hours"],
                                          publish_windows=cfg["publish_windows"],
                                          read_history=cfg["read_history"],
                                          history_max_posts=cfg["history_max_posts"],
                                          fresh_window_min=cfg["fresh_window_min"],
                                          rewrite_enabled=True if cfg["rewrite"] is None else bool(cfg["rewrite"]),
                                          dup_recap_enabled=bool(cfg["dup_recap"]),
                                          editorial=bool(cfg["editorial"]),
                                          no_review=bool(cfg["no_review"]),
                                          aggregate_mode=cfg["aggregate_mode"],
                                          autopilot_sig_guard=bool(cfg["autopilot_sig_guard"]),
                                          aggregate_min_score=cfg["aggregate_min_score"],
                                          aggregate_accept=cfg["aggregate_accept"],
                                          aggregate_reject=cfg["aggregate_reject"],
                                          llm_instructions=cfg["llm_instructions"],
                                          classify_model=cfg["classify_model"],
                                          dedup_enabled=bool(cfg["dedup"]),
                                          autopilot=bool(cfg["autopilot"]),
                                          autopilot_min_score=cfg["autopilot_min_score"],
                                          review_if_uncertain=True if cfg["review_if_uncertain"] is None else bool(cfg["review_if_uncertain"]),
                                          double_check=bool(cfg["double_check"]),
                                          double_check_online=bool(cfg["double_check_online"]),
                                          double_check_fact_strictness=cfg["double_check_fact_strictness"],
                                          style_profile_id=style_id))'''
s = s[:start] + new + s[end:]
p.write_text(s, encoding="utf-8")
print("create-block rewritten")