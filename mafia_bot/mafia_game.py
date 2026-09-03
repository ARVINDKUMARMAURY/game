"""
Per-chat Mafia game state. One MafiaGame instance per group chat_id, stored
in a dict in bot.py — so multiple groups can run games at the same time
without stepping on each other.
"""

import random
from enum import Enum

MIN_PLAYERS = 5
DAY_SECONDS = 2 * 60
NIGHT_SECONDS = 60

# Special (non-Citizen) roles unlocked by player count. Remaining slots fill with Citizen.
SPECIAL_ROLES = {
    5: ["Mafia", "Doctor", "Detective"],
    6: ["Mafia", "Doctor", "Detective"],
    7: ["Mafia", "Mafia", "Doctor", "Detective"],
    8: ["Mafia", "Mafia", "Doctor", "Detective", "Vigilante"],
    9: ["Mafia", "Mafia", "Doctor", "Detective", "Vigilante", "Jester"],
    10: ["Mafia", "Mafia", "Mafia", "Doctor", "Detective", "Vigilante", "Jester"],
}


class Phase(Enum):
    LOBBY = "lobby"
    DAY = "day"
    NIGHT = "night"
    ENDED = "ended"


class Player:
    def __init__(self, user_id, name):
        self.user_id = user_id
        self.name = name
        self.role = None  # "Mafia" | "Doctor" | "Detective" | "Vigilante" | "Jester" | "Citizen"
        self.alive = True


def build_roles(n_players):
    n = min(n_players, max(SPECIAL_ROLES.keys()))
    roles = list(SPECIAL_ROLES[n])
    while len(roles) < n_players:
        roles.append("Citizen")
    random.shuffle(roles)
    return roles


class MafiaGame:
    def __init__(self, chat_id, host_id):
        self.chat_id = chat_id
        self.host_id = host_id
        self.phase = Phase.LOBBY
        self.players: dict[int, Player] = {}

        self.votes: dict[int, int] = {}          # voter_id -> target_id (day)

        self.night_kill_target = None             # mafia's chosen target
        self.night_save_target = None
        self.night_investigate_target = None
        self.night_vig_target = None

        self.mafia_ids = set()
        self.doctor_id = None
        self.detective_id = None
        self.vigilante_id = None
        self.jester_id = None
        self.vigilante_used = False

    # ---------- Lobby ----------

    def add_player(self, user_id, name):
        if self.phase != Phase.LOBBY:
            return False, "Game already started."
        if user_id in self.players:
            return False, "You already joined."
        self.players[user_id] = Player(user_id, name)
        return True, None

    def remove_player(self, user_id):
        if user_id in self.players:
            del self.players[user_id]
            return True
        return False

    def can_start(self):
        return len(self.players) >= MIN_PLAYERS

    # ---------- Start ----------

    def assign_roles(self):
        ids = list(self.players.keys())
        roles = build_roles(len(ids))
        for uid, role in zip(ids, roles):
            p = self.players[uid]
            p.role = role
            if role == "Mafia":
                self.mafia_ids.add(uid)
            elif role == "Doctor":
                self.doctor_id = uid
            elif role == "Detective":
                self.detective_id = uid
            elif role == "Vigilante":
                self.vigilante_id = uid
            elif role == "Jester":
                self.jester_id = uid
        self.phase = Phase.DAY

    def alive_players(self):
        return [p for p in self.players.values() if p.alive]

    def alive_mafia(self):
        return [uid for uid in self.mafia_ids if self.players[uid].alive]

    def alive_non_mafia(self):
        return [p for p in self.alive_players() if p.role != "Mafia"]

    # ---------- Day voting ----------

    def start_day(self):
        self.phase = Phase.DAY
        self.votes = {}

    def cast_vote(self, voter_id, target_id):
        if self.phase != Phase.DAY:
            return False, "Voting isn't open right now."
        voter = self.players.get(voter_id)
        target = self.players.get(target_id)
        if not voter or not voter.alive:
            return False, "Only alive players can vote."
        if not target or not target.alive:
            return False, "Invalid target."
        if voter_id == target_id:
            return False, "You can't vote for yourself."
        self.votes[voter_id] = target_id
        return True, None

    def all_voted(self):
        alive_ids = [p.user_id for p in self.alive_players()]
        return len(alive_ids) > 0 and all(uid in self.votes for uid in alive_ids)

    def tally_day_votes(self):
        """Returns (eliminated_id_or_None, counts, is_tie)."""
        counts = {}
        for target_id in self.votes.values():
            counts[target_id] = counts.get(target_id, 0) + 1
        if not counts:
            return None, counts, False
        max_votes = max(counts.values())
        top = [uid for uid, c in counts.items() if c == max_votes]
        if len(top) > 1:
            return None, counts, True
        eliminated = top[0]
        self.players[eliminated].alive = False
        return eliminated, counts, False

    def check_jester_win(self, eliminated_id):
        return eliminated_id is not None and self.jester_id == eliminated_id

    # ---------- Night actions ----------

    def start_night(self):
        self.phase = Phase.NIGHT
        self.night_kill_target = None
        self.night_save_target = None
        self.night_investigate_target = None
        self.night_vig_target = None

    def submit_kill(self, mafia_id, target_id):
        if self.phase != Phase.NIGHT or mafia_id not in self.mafia_ids:
            return False, "Not allowed."
        if not self.players.get(target_id) or not self.players[target_id].alive:
            return False, "Invalid target."
        self.night_kill_target = target_id
        return True, None

    def submit_save(self, doctor_id, target_id):
        if self.phase != Phase.NIGHT or doctor_id != self.doctor_id:
            return False, "Not allowed."
        if not self.players.get(target_id) or not self.players[target_id].alive:
            return False, "Invalid target."
        self.night_save_target = target_id
        return True, None

    def submit_investigate(self, detective_id, target_id):
        if self.phase != Phase.NIGHT or detective_id != self.detective_id:
            return False, "Not allowed."
        if not self.players.get(target_id):
            return False, "Invalid target."
        self.night_investigate_target = target_id
        return True, self.players[target_id].role

    def submit_vig_kill(self, vig_id, target_id):
        if self.phase != Phase.NIGHT or vig_id != self.vigilante_id:
            return False, "Not allowed."
        if self.vigilante_used:
            return False, "You already used your one bullet."
        if not self.players.get(target_id) or not self.players[target_id].alive:
            return False, "Invalid target."
        self.night_vig_target = target_id
        self.vigilante_used = True
        return True, None

    def resolve_night(self):
        """
        Applies mafia kill, doctor save, and vigilante shot (with guilt-death if the
        vigilante kills a non-Mafia player). Returns a list of (user_id, reason) for
        everyone who died, reason in {"mafia", "vigilante", "guilt"}.
        """
        eliminated = []
        killed = self.night_kill_target
        saved = self.night_save_target

        if killed is not None and killed != saved and self.players[killed].alive:
            self.players[killed].alive = False
            eliminated.append((killed, "mafia"))

        vig_target = self.night_vig_target
        if vig_target is not None and vig_target != saved and self.players[vig_target].alive:
            self.players[vig_target].alive = False
            eliminated.append((vig_target, "vigilante"))
            if self.players[vig_target].role != "Mafia" and self.vigilante_id and self.players[self.vigilante_id].alive:
                self.players[self.vigilante_id].alive = False
                eliminated.append((self.vigilante_id, "guilt"))

        return eliminated

    # ---------- Win check ----------

    def check_winner(self):
        mafia_alive = len(self.alive_mafia())
        others_alive = len(self.alive_non_mafia())
        if mafia_alive == 0:
            return "citizens"
        if mafia_alive >= others_alive:
            return "mafia"
        return None

    def end_game(self):
        self.phase = Phase.ENDED
