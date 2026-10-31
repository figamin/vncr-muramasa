"""
LJTA (Learn Japanese Through Anime) - Quiz Roles Cog
Six Kotoba quizzes (N5 → Insane). Passing one grants its role.

This file replaces the old hiragana verification quiz. It keeps the name
verification.py so main.py loads it instead of the parent bot's verification cog.
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import re
import requests
from pathlib import Path


# ---------------------------------------------------------------------------
# QUIZ DEFINITIONS - edit names, messages and requirements here.
#
# Placeholders you can use in messages:
#   {user}    - mention of the quiz taker
#   {role}    - the quiz's level name as plain text, e.g. N5 (never pings the role)
#   {name}    - the quiz's display name
#   {command} - the quiz command
#   {score}   - required score
#   {issues}  - (failure_message only) bullet list of what went wrong
# ---------------------------------------------------------------------------
QUIZZES = [
    {
        "key": "n5",
        "level": "N5",
        "name": "N5 Quiz",
        "emoji": "🌱",
        "command": "k!q n5+10kn5 20 font=1 atl=10 mmq=5 nd",
        "decks": {"n5", "10kn5"},
        "score": 20,
        "time_limit_ms": 10000,
        "max_missed": 5,
        "role_id": 1422237639523962900,
        "description": "Beginner vocabulary (JLPT N5).",
        "start_message": "🌱 N5 QUIZ started by {user}! Reach {score} points to earn {role}. Good luck! 🍀",
        "success_message": "🎉 {user} passed the N5 Quiz and earned {role}!",
        "announcement_message": "🌱 Congratulations {user} on passing the N5 Quiz! You now have {role}.",
        "failure_message": "❌ {user}, your N5 Quiz didn't meet the requirements:\n{issues}\n\nYou can try again anytime! Use `/quizzes` to see the commands.",
    },
    {
        "key": "n4",
        "level": "N4",
        "name": "N4 Quiz",
        "emoji": "🌿",
        "command": "k!q n4+10kn4 35 font=1 atl=10 mmq=5 nd",
        "decks": {"n4", "10kn4"},
        "score": 35,
        "time_limit_ms": 10000,
        "max_missed": 5,
        "role_id": 1422237721887248506,
        "description": "Elementary vocabulary (JLPT N4).",
        "start_message": "🌿 N4 QUIZ started by {user}! Reach {score} points to earn {role}. Good luck! 🍀",
        "success_message": "🎉 {user} passed the N4 Quiz and earned {role}!",
        "announcement_message": "🌿 Congratulations {user} on passing the N4 Quiz! You now have {role}.",
        "failure_message": "❌ {user}, your N4 Quiz didn't meet the requirements:\n{issues}\n\nYou can try again anytime! Use `/quizzes` to see the commands.",
    },
    {
        "key": "n3",
        "level": "N3",
        "name": "N3 Quiz",
        "emoji": "🌳",
        "command": "k!q n3+10kn3 50 font=1 atl=10 mmq=5 nd",
        "decks": {"n3", "10kn3"},
        "score": 50,
        "time_limit_ms": 10000,
        "max_missed": 5,
        "role_id": 1422237837163757660,
        "description": "Intermediate vocabulary (JLPT N3).",
        "start_message": "🌳 N3 QUIZ started by {user}! Reach {score} points to earn {role}. Good luck! 🍀",
        "success_message": "🎉 {user} passed the N3 Quiz and earned {role}!",
        "announcement_message": "🌳 Congratulations {user} on passing the N3 Quiz! You now have {role}.",
        "failure_message": "❌ {user}, your N3 Quiz didn't meet the requirements:\n{issues}\n\nYou can try again anytime! Use `/quizzes` to see the commands.",
    },
    {
        "key": "n2",
        "level": "N2",
        "name": "N2 Quiz",
        "emoji": "⛰️",
        "command": "k!q n2+10kn2 50 font=1 atl=10 mmq=5 nd",
        "decks": {"n2", "10kn2"},
        "score": 50,
        "time_limit_ms": 10000,
        "max_missed": 5,
        "role_id": 1422237919044829404,
        "description": "Upper-intermediate vocabulary (JLPT N2).",
        "start_message": "⛰️ N2 QUIZ started by {user}! Reach {score} points to earn {role}. Good luck! 🍀",
        "success_message": "🎉 {user} passed the N2 Quiz and earned {role}!",
        "announcement_message": "⛰️ Congratulations {user} on passing the N2 Quiz! You now have {role}.",
        "failure_message": "❌ {user}, your N2 Quiz didn't meet the requirements:\n{issues}\n\nYou can try again anytime! Use `/quizzes` to see the commands.",
    },
    {
        "key": "n1",
        "level": "N1",
        "name": "N1 Quiz",
        "emoji": "🗻",
        "command": "k!q n1+10kn1 50 font=1 atl=10 mmq=5 nd",
        "decks": {"n1", "10kn1"},
        "score": 50,
        "time_limit_ms": 10000,
        "max_missed": 5,
        "role_id": 1422237945787846878,
        "description": "Advanced vocabulary (JLPT N1).",
        "start_message": "🗻 N1 QUIZ started by {user}! Reach {score} points to earn {role}. Good luck! 🍀",
        "success_message": "🎉 {user} passed the N1 Quiz and earned {role}!",
        "announcement_message": "🗻 Congratulations {user} on passing the N1 Quiz! You now have {role}.",
        "failure_message": "❌ {user}, your N1 Quiz didn't meet the requirements:\n{issues}\n\nYou can try again anytime! Use `/quizzes` to see the commands.",
    },
    {
        "key": "insane",
        "level": "N0",
        "name": "Insane Quiz",
        "emoji": "🔥",
        "command": "k!q insane+haard 50 font=1 atl=10 mmq=5 nd",
        "decks": {"insane", "haard"},
        "score": 50,
        "time_limit_ms": 10000,
        "max_missed": 5,
        "role_id": 1422237977060442324,
        "description": "Beyond N1. Only for the brave.",
        "start_message": "🔥 INSANE QUIZ started by {user}! Reach {score} points to earn {role}. Good luck... you'll need it! 💀",
        "success_message": "🏆 {user} conquered the Insane Quiz and earned {role}!",
        "announcement_message": "🔥 Incredible! {user} passed the Insane Quiz and earned {role}!",
        "failure_message": "❌ {user}, your Insane Quiz didn't meet the requirements:\n{issues}\n\nYou can try again anytime! Use `/quizzes` to see the commands.",
    },
]

# Channel where "someone passed" announcements are posted.
ANNOUNCEMENT_CHANNEL_ID = 1415732490815016972

# Shown when someone starts a quiz whose role they already have.
ALREADY_HAVE_ROLE_START_MESSAGE = "🔄 {name} started by {user} - you already have {role}!"


def _normalize_decks(decks) -> frozenset:
    return frozenset(d.strip().lower() for d in decks if d and d.strip())


def _required_tokens(quiz: dict) -> set:
    """Everything in the quiz command after the decks, e.g. {"20", "font=1", "atl=10", "mmq=5", "nd"}."""
    return set(quiz["command"].lower().split()[2:])


class VerificationCog(commands.Cog):
    """Kotoba quiz → role cog for the anime bot"""

    KOTOBA_ID = 251239170058616833

    def __init__(self, bot):
        self.bot = bot
        self.data_dir = Path(__file__).parent.parent / 'data'
        self.data_dir.mkdir(exist_ok=True)
        self.data_file = self.data_dir / 'quiz_roles_data.json'
        self.data = self.load_data()
        # Look up quizzes by their exact deck set
        self.quizzes_by_decks = {_normalize_decks(q["decks"]): q for q in QUIZZES}

    # ---------------- data (who passed what, for restoring on rejoin) ----------------

    def load_data(self) -> dict:
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                if isinstance(data.get("passed"), dict):
                    return data
            except (json.JSONDecodeError, OSError) as e:
                print(f"[Anime Quiz] Error loading quiz data: {e}")
        return {"passed": {q["key"]: [] for q in QUIZZES}}

    def save_data(self):
        try:
            with open(self.data_file, 'w') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"[Anime Quiz] Error saving quiz data: {e}")

    def has_passed(self, quiz: dict, user_id: int) -> bool:
        return str(user_id) in self.data["passed"].get(quiz["key"], [])

    def record_pass(self, quiz: dict, user_id: int):
        passed = self.data["passed"].setdefault(quiz["key"], [])
        if str(user_id) not in passed:
            passed.append(str(user_id))
            self.save_data()

    # ---------------- helpers ----------------

    @staticmethod
    def format_message(template: str, quiz: dict, member: discord.Member, issues: str = "") -> str:
        return template.format(
            user=member.mention,
            role=quiz["level"],
            name=quiz["name"],
            command=quiz["command"],
            score=quiz["score"],
            issues=issues,
        )

    def get_announcement_channel(self, guild: discord.Guild):
        return guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)

    def find_quiz_for_command(self, content: str):
        """Return the quiz a k!q / k!quiz command is for, or None."""
        parts = content.split()
        if len(parts) < 2 or parts[0].lower() not in ("k!q", "k!quiz"):
            return None
        return self.quizzes_by_decks.get(_normalize_decks(parts[1].split('+')))

    def find_quiz_for_report(self, report_data: dict):
        decks = _normalize_decks(d.get('shortName', '') for d in report_data.get('decks', []))
        return self.quizzes_by_decks.get(decks)

    @staticmethod
    def check_requirements(quiz: dict, report_data: dict) -> list:
        """Return a list of unmet requirements (empty list = passed)."""
        issues = []
        scores = report_data.get('scores', [])
        score = scores[0].get('score', 0) if scores else 0
        settings = report_data.get('settings', {})

        if score < quiz["score"]:
            issues.append(f"Score too low: got {score}, need {quiz['score']}")
        if settings.get('answerTimeLimitInMs', 0) != quiz["time_limit_ms"]:
            issues.append(f"Wrong time limit: use `atl={quiz['time_limit_ms'] // 1000}`")
        if settings.get('maxMissedQuestions', 0) != quiz["max_missed"]:
            issues.append(f"Wrong max missed: use `mmq={quiz['max_missed']}`")
        # nd = no delay after an answered question (same check the main bot uses)
        if "nd" in _required_tokens(quiz) and settings.get('newQuestionDelayAfterAnsweredInMs') != 0:
            issues.append("Missing `nd`")
        # Kotoba doesn't store font=N in a usable form, so check the command as typed
        raw_command = report_data.get('rawStartCommand')
        if raw_command is not None:
            typed = set(raw_command.lower().split())
            for token in sorted(t for t in _required_tokens(quiz) if t.startswith("font=")):
                if token not in typed:
                    issues.append(f"Missing `{token}`")
        return issues

    # ---------------- quiz flow ----------------

    async def check_quiz_start(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        quiz = self.find_quiz_for_command(message.content)
        if not quiz:
            return

        role = message.guild.get_role(quiz["role_id"])
        if not role:
            return  # a server that doesn't have these roles
        # Stay quiet if the command is missing settings; the failure message explains later
        if not _required_tokens(quiz).issubset(message.content.lower().split()[2:]):
            return

        if role in message.author.roles:
            text = ALREADY_HAVE_ROLE_START_MESSAGE
        else:
            text = quiz["start_message"]
        await message.channel.send(self.format_message(text, quiz, message.author))

    async def grant_quiz_role(self, message: discord.Message, quiz: dict, member: discord.Member, role: discord.Role):
        if role in member.roles:
            self.record_pass(quiz, member.id)
            return

        try:
            await member.add_roles(role, reason=f"Passed {quiz['name']}")
        except discord.Forbidden:
            await message.channel.send(f"Error: I can't assign the {quiz['level']} role - missing permissions or the role is above mine.")
            return

        self.record_pass(quiz, member.id)
        await message.channel.send(self.format_message(quiz["success_message"], quiz, member))

        announce = self.get_announcement_channel(message.guild)
        if announce and announce.id != message.channel.id:
            try:
                await announce.send(self.format_message(quiz["announcement_message"], quiz, member))
            except discord.Forbidden:
                print(f"[Anime Quiz] Cannot send to announcement channel in {message.guild.name}")

        print(f"[Anime Quiz] {member.name} ({member.id}) passed {quiz['name']} in {message.guild.name}")

    async def process_quiz_report(self, message: discord.Message, report_id: str):
        try:
            url = f'https://kotobaweb.com/api/game_reports/{report_id}'
            try:
                response = await asyncio.to_thread(requests.get, url, timeout=10)
            except requests.RequestException as e:
                print(f"[Anime Quiz] Request to Kotoba failed: {e}")
                return
            if response.status_code != 200:
                print(f"[Anime Quiz] Failed to fetch quiz report: {response.status_code}")
                return
            report_data = response.json()

            quiz = self.find_quiz_for_report(report_data)
            if not quiz:
                return  # not one of our quizzes

            role = message.guild.get_role(quiz["role_id"])
            if not role:
                return  # a server that doesn't have these roles

            participants = report_data.get('participants', [])
            if len(participants) != 1:
                print("[Anime Quiz] Quiz has multiple participants, skipping")
                return

            member = message.guild.get_member(int(participants[0]['discordUser']['id']))
            if not member:
                return

            issues = self.check_requirements(quiz, report_data)
            if issues:
                if role in member.roles:
                    return  # already has it, no need to nag
                issues_text = "\n".join(f"• {i}" for i in issues)
                await message.channel.send(self.format_message(quiz["failure_message"], quiz, member, issues_text))
                return

            await self.grant_quiz_role(message, quiz, member, role)

        except Exception as e:
            print(f"[Anime Quiz] Error processing quiz report: {e}")
            import traceback
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != self.KOTOBA_ID:
            if message.content.lower().startswith('k!q'):
                await self.check_quiz_start(message)
            return

        if not message.embeds or not message.guild:
            return

        # Collect report IDs from embed descriptions and fields (each ID once)
        report_ids = []
        for embed in message.embeds:
            texts = [embed.description or ""] + [field.value or "" for field in embed.fields]
            for text in texts:
                for report_id in re.findall(r'game_reports/([A-Za-z0-9_-]+)', text):
                    if report_id not in report_ids:
                        report_ids.append(report_id)

        for report_id in report_ids:
            await self.process_quiz_report(message, report_id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Give returning members back the quiz roles they already earned."""
        if member.bot:
            return
        roles = []
        for quiz in QUIZZES:
            if self.has_passed(quiz, member.id):
                role = member.guild.get_role(quiz["role_id"])
                if role and role not in member.roles:
                    roles.append(role)
        if roles:
            try:
                await member.add_roles(*roles, reason="Restored quiz roles on rejoin")
                print(f"[Anime Quiz] Restored {len(roles)} quiz role(s) to {member.name}")
            except discord.Forbidden:
                print(f"[Anime Quiz] Cannot restore quiz roles to {member.name}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Keep the saved data in step with roles a moderator adds or removes by hand."""
        if before.roles == after.roles:
            return
        changed = False
        for quiz in QUIZZES:
            role = after.guild.get_role(quiz["role_id"])
            if not role:
                continue
            had, has = role in before.roles, role in after.roles
            passed = self.data["passed"].setdefault(quiz["key"], [])
            user_id = str(after.id)
            if has and not had and user_id not in passed:
                passed.append(user_id)
                changed = True
            elif had and not has and user_id in passed:
                passed.remove(user_id)
                changed = True
        if changed:
            self.save_data()

    # ---------------- slash commands ----------------

    @app_commands.command(name="quizzes", description="Show the level quizzes and the roles they give")
    async def quizzes(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 Level Quizzes",
            description=(
                "Pass a Kotoba quiz to earn its level role!\n"
                "Copy a command below exactly as it is. Unlimited tries, no cooldown."
            ),
            color=discord.Color.blue(),
        )
        for quiz in QUIZZES:
            embed.add_field(
                name=f"{quiz['emoji']} {quiz['name']}",
                value=(
                    f"{quiz['description']}\n"
                    f"Role: {quiz['level']} • Score: {quiz['score']} • "
                    f"{quiz['time_limit_ms'] // 1000}s per question • max {quiz['max_missed']} missed\n"
                    f"```{quiz['command']}```"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(VerificationCog(bot))
