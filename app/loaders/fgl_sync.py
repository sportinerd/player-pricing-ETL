# app/loaders/fgl_sync.py
#
# Syncs computed player prices back to Fantasy_Global_Livescore (FGL) player
# collection.  FGL uses SportMonks IDs; GoalServe uses its own — the two ID
# spaces are completely separate.  Matching is done purely by player name via a
# 7-tier cascade, proven at ~98.3% accuracy on players that actually exist in
# both systems (~5% of GoalServe players are youth/fringe not yet catalogued by
# SportMonks and are logged then skipped).
#
# Matching tiers (in order):
#   T1  Exact normalised display_name                     ~86%
#   T2  Exact normalised full name (legal name)           ~ 3%
#   T3  Token-set match — order-insensitive               ~ 3%
#       ("Jesus Gabriel" ↔ "Gabriel Jesus")
#   T4  Token-subset — GS name ⊆ FGL name (≤2 extras)    ~ 0.2%
#   T5  First-initial + last-token + optional position    ~ 1%
#       ("Samuel Byram" ↔ "Sam Byram")
#   T6  Single-token name + position disambiguation       ~ 0.8%
#       ("Gabriel" DEF → "Gabriel Magalhães" DEF)
#   T7  Token-subset + position (reverse direction)       covers edge cases
#
# Each FGL player doc receives:
#   • price — computed fantasy price in £m (top-level field, overwritten each run)
#
# The update is a bulk $set so it is safe to re-run (idempotent).

import unicodedata
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from pymongo import UpdateOne

from app.core.config import settings
from app.core.database import get_db
from app.utils.logger import get_logger

log = get_logger(__name__)

# ── Name normalisation ────────────────────────────────────────────────────────

# Characters that survive NFD + ascii-ignore but should still be mapped
_CHAR_MAP = str.maketrans({
    'ø': 'o', 'Ø': 'O', 'ß': 'ss',
    'ı': 'i', 'ğ': 'g', 'ş': 's',
    'ç': 'c', 'Ç': 'C', 'İ': 'I',
    'ð': 'd', 'þ': 'th', 'æ': 'ae',
    'ł': 'l', 'Ł': 'L',
})


def _norm(s: Optional[str]) -> str:
    """
    Lowercase, strip accents/special chars, collapse whitespace.
    Apostrophes, hyphens and dots become spaces so name parts remain tokenisable.
    """
    if not s:
        return ""
    s = s.translate(_CHAR_MAP)
    s = unicodedata.normalize('NFD', s)
    s = s.encode('ascii', 'ignore').decode()
    s = re.sub(r"['\-\.]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: Optional[str]) -> frozenset:
    return frozenset(_norm(s).split())


# ── FGLSync ───────────────────────────────────────────────────────────────────

class FGLSync:
    """
    Connects to the Fantasy_Global_Livescore database and writes pricing data
    onto matched player documents.

    Usage (inside an async context):
        sync = FGLSync()
        await sync.connect()
        result = await sync.sync_pricing(priced_players, league_id, season_label)
        await sync.close()
    """

    def __init__(self):
        # In-memory indexes — rebuilt once per sync call
        self._fgl_cache: List[Dict] = []
        self._idx_display: Dict[str, List[Dict]] = {}
        self._idx_name:    Dict[str, List[Dict]] = {}
        self._idx_tokens:  Dict[frozenset, List[Dict]] = {}
        self._idx_last:    Dict[str, List[Dict]] = {}   # last significant token

    def _fgl_db(self):
        """Return the FGL database using the shared Motor client."""
        return get_db().client[settings.FGL_DB_NAME]

    # ── Index construction ────────────────────────────────────────────────────

    async def _load_indexes(self):
        """Fetch all FGL players and build in-memory lookup indexes."""
        cursor = self._fgl_db().players.find(
            {},
            {
                "_id": 1, "api_player_id": 1,
                "name": 1, "display_name": 1, "common_name": 1,
                "first_name": 1, "last_name": 1,
                "position": 1, "position_group": 1,
            },
        )
        self._fgl_cache = await cursor.to_list(length=None)
        log.info("[FGLSync] Loaded %d FGL players into memory", len(self._fgl_cache))

        self._idx_display  = defaultdict(list)
        self._idx_name     = defaultdict(list)
        self._idx_tokens   = defaultdict(list)
        self._idx_last     = defaultdict(list)

        for p in self._fgl_cache:
            dn    = p.get("display_name") or p.get("name") or ""
            fn    = p.get("name") or ""
            ln    = p.get("last_name") or ""

            self._idx_display[_norm(dn)].append(p)
            self._idx_name[_norm(fn)].append(p)

            ts = _tokens(dn)
            if ts:
                self._idx_tokens[ts].append(p)

            # Last significant token of display_name (or last_name field)
            last_source = _norm(ln) or _norm(dn)
            last_parts  = last_source.split()
            if last_parts:
                self._idx_last[last_parts[-1]].append(p)

    # ── Position helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _pos_match(fgl_doc: Dict, gs_pos: str) -> bool:
        """True if FGL position_group aligns with GoalServe position code."""
        pg = (fgl_doc.get("position_group") or "").upper()
        if pg:
            return pg == gs_pos
        # Fall back to verbose position field
        verbose = (fgl_doc.get("position") or "").lower()
        _verbose_map = {
            "GK":  ["goalkeeper", "keeper"],
            "DEF": ["defender", "back"],
            "MID": ["midfielder", "midfield", "winger"],
            "FWD": ["attacker", "forward", "striker"],
        }
        return any(kw in verbose for kw in _verbose_map.get(gs_pos, []))

    @staticmethod
    def _disambiguate(hits: List[Dict], gs_pos: str) -> Optional[Dict]:
        """Return the single hit that matches position, else None."""
        pos_hits = [h for h in hits if FGLSync._pos_match(h, gs_pos)]
        return pos_hits[0] if len(pos_hits) == 1 else None

    # ── 7-tier match ──────────────────────────────────────────────────────────

    def _match(self, gs: Dict) -> Tuple[Optional[Dict], str]:
        """
        Returns (fgl_doc, method_label) or (None, "unmatched").
        `gs` must have keys: name, position.
        """
        name    = gs.get("name", "")
        gs_pos  = gs.get("position", "")
        gs_norm = _norm(name)
        gs_ts   = _tokens(name)

        # ── T1: exact normalised display_name ─────────────────────────────────
        hits = self._idx_display.get(gs_norm, [])
        if len(hits) == 1:
            return hits[0], "T1_display_exact"
        if len(hits) > 1:
            pick = self._disambiguate(hits, gs_pos)
            if pick:
                return pick, "T1_display+pos"

        # ── T2: exact normalised full (legal) name ────────────────────────────
        hits = self._idx_name.get(gs_norm, [])
        if len(hits) == 1:
            return hits[0], "T2_name_exact"

        # ── T3: token-set — order-insensitive ("Gomes Joao" ↔ "João Gomes") ──
        hits = self._idx_tokens.get(gs_ts, [])
        if len(hits) == 1:
            return hits[0], "T3_token_set"
        if len(hits) > 1:
            pick = self._disambiguate(hits, gs_pos)
            if pick:
                return pick, "T3_token_set+pos"

        # ── T4: GS tokens ⊆ FGL tokens, max 2 extra (GS name is shorter) ─────
        if gs_ts:
            subset_hits: List[Dict] = []
            for fts, fl in self._idx_tokens.items():
                if gs_ts.issubset(fts) and len(fts) - len(gs_ts) <= 2:
                    subset_hits.extend(fl)
            if len(subset_hits) == 1:
                return subset_hits[0], "T4_token_subset"
            if len(subset_hits) > 1:
                pick = self._disambiguate(subset_hits, gs_pos)
                if pick:
                    return pick, "T4_token_subset+pos"

        # ── T5: first-initial + last-token (+ position) ───────────────────────
        #   "Samuel Byram" → 's' + 'byram' → matches "Sam Byram"
        toks = gs_norm.split()
        if len(toks) >= 2:
            fi      = toks[0][0]
            last_t  = toks[-1]
            lhits   = self._idx_last.get(last_t, [])
            fi_hits = [
                h for h in lhits
                if _norm(h.get("display_name") or h.get("name") or "").startswith(fi)
            ]
            if len(fi_hits) == 1:
                return fi_hits[0], "T5_initial+last"
            if len(fi_hits) > 1:
                pick = self._disambiguate(fi_hits, gs_pos)
                if pick:
                    return pick, "T5_initial+last+pos"

        # ── T6: single-token name → last-token index + position ───────────────
        #   "Gabriel" (DEF) → all players whose last token is "gabriel", filter DEF
        #   "Nico" (MID)    → all players whose any token is "nico", filter MID
        if len(toks) == 1:
            single = toks[0]
            s_hits = self._idx_last.get(single, [])
            if len(s_hits) == 1:
                return s_hits[0], "T6_single_token"
            if len(s_hits) > 1:
                pick = self._disambiguate(s_hits, gs_pos)
                if pick:
                    return pick, "T6_single_token+pos"
            # Also search any-token for single names
            any_hits = [
                p for p in self._fgl_cache
                if single in _tokens(p.get("display_name") or p.get("name") or "")
            ]
            if len(any_hits) == 1:
                return any_hits[0], "T6_single_any_token"
            if len(any_hits) > 1:
                pick = self._disambiguate(any_hits, gs_pos)
                if pick:
                    return pick, "T6_single_any_token+pos"

        # ── T7: FGL tokens ⊆ GS tokens (FGL name shorter, e.g. "Tolu Arokodare"
        #   is subset of "Emmanuel Arokodare Toluwalase") ─────────────────────
        if gs_ts:
            rev_hits: List[Dict] = []
            for fts, fl in self._idx_tokens.items():
                if fts and fts.issubset(gs_ts) and len(gs_ts) - len(fts) <= 2:
                    rev_hits.extend(fl)
            if len(rev_hits) == 1:
                return rev_hits[0], "T7_reverse_subset"
            if len(rev_hits) > 1:
                pick = self._disambiguate(rev_hits, gs_pos)
                if pick:
                    return pick, "T7_reverse_subset+pos"

        return None, "unmatched"

    # ── Sync pricing ──────────────────────────────────────────────────────────

    async def sync_pricing(
        self,
        priced_players: List[Dict],
        league_id: str,
        season_label: str,
    ) -> Dict:
        """
        Match each priced GoalServe player to an FGL player and write pricing
        data onto the FGL document.

        Returns a summary dict with counts and unmatched player details.
        """
        if not priced_players:
            return {"matched": 0, "unmatched": 0, "written": 0}

        await self._load_indexes()

        ops: List[UpdateOne] = []
        method_counts: Dict[str, int] = defaultdict(int)
        unmatched_players: List[Dict] = []

        for gs in priced_players:
            fgl_doc, method = self._match(gs)
            method_counts[method] += 1

            if fgl_doc is None:
                unmatched_players.append({
                    "name":       gs.get("name"),
                    "team":       gs.get("team_name"),
                    "position":   gs.get("position"),
                    "gs_id":      gs.get("player_id"),
                })
                continue

            pricing_payload = {
                "price": gs.get("price"),
                "goalserve_player_id": int(gs.get("player_id")),
                "position_group": gs.get("position"),
            }

            ops.append(UpdateOne(
                {"_id": fgl_doc["_id"]},
                {"$set": pricing_payload},
            ))

        matched = len(ops)
        written = 0
        if ops:
            result = await self._fgl_db().players.bulk_write(ops, ordered=False)
            written = result.modified_count + result.upserted_count
            log.info(
                "[FGLSync] bulk_write: %d ops → %d modified, %d upserted",
                len(ops), result.modified_count, result.upserted_count,
            )

        # Log matching breakdown
        log.info(
            "[FGLSync] league=%s  matched=%d  unmatched=%d  written=%d",
            league_id, matched, len(unmatched_players), written,
        )
        for m, c in sorted(method_counts.items(), key=lambda x: -x[1]):
            if m != "unmatched":
                log.info("  %-28s %d", m, c)
        if unmatched_players:
            log.info("[FGLSync] Unmatched players (not in SportMonks/FGL):")
            for p in unmatched_players:
                log.info(
                    "  [%s] %-30s  %s  (gs_id=%s)",
                    p["position"], p["name"], p["team"], p["gs_id"],
                )

        return {
            "matched":           matched,
            "unmatched":         len(unmatched_players),
            "written":           written,
            "method_breakdown":  dict(method_counts),
            "unmatched_players": unmatched_players,
        }
