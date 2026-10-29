import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from dotenv import load_dotenv
import os, libsql, asyncio
from datetime import datetime, timedelta, timezone, time
from keep_alive import keep_alive

load_dotenv()
token = os.getenv('BOT_TOKEN')

TEST_MODE = False
if TEST_MODE == False:
    keep_alive()

# Logging Handler
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# Bot's Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Bot
bot = commands.Bot(command_prefix='!', intents=intents)

# SQLite Database Setup
DB_FILE = "timed_roles.db"
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

db = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

# database helper functions

async def db_fetch(query: str, params: tuple = (), fetch_all: bool = False):
    def _run():
        cursor = db.execute(query, params)
        return cursor.fetchall() if fetch_all else cursor.fetchone()
    return await asyncio.to_thread(_run)

async def db_execute(query: str, params: tuple = (), commit: bool = True):
    def _run():
        db.execute(query, params)
        if commit:
            db.commit()
    await asyncio.to_thread(_run)

async def db_commit():
    await asyncio.to_thread(db.commit)

async def init_db():
    '''Create database table and indexes (if not yet existing)'''
    try:
        # Create main timed roles table:
        await db_execute("""
            CREATE TABLE IF NOT EXISTS timed_roles (
                user_id TEXT, 
                guild_id TEXT,
                role_id TEXT,
                end_time TEXT,
                PRIMARY KEY (user_id, guild_id, role_id)
            )
        """, commit=False)
        
        # Create table for Guild (Server) configs:
        await db_execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                role_log_channel_id TEXT
            )
        """)
        print("Turso Database Initialized")
    except Exception as e:
        print(f"Database Initialization Error: {e}")

async def is_bot_owner(interaction: discord.Interaction) -> bool:
    # Check if user sending command is bot owner
    return await interaction.client.is_owner(interaction.user)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Error message for required role missing to run command
    if isinstance(error, app_commands.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send("You do not have permission to run this command.", ephemeral=True)
        else:
            await interaction.response.send_message("You do not have permission to run this command.", ephemeral=True)
    else:
        print(f"[ERROR] Slash command error occurred: {error}")

@bot.event
async def on_ready():
    # Start up bot
    print(f"Logged in as {bot.user.name}")
    await init_db()
    if not daily_expiry_check.is_running():
        daily_expiry_check.start()
        print("[INFO] Started daily role expiry check task loop.")

@bot.command()
@commands.is_owner()
async def sync(ctx):
    await bot.tree.sync()
    await ctx.send("Slash commands synced")

@bot.command()
@commands.is_owner()
async def syncserver(ctx):
    await bot.tree.sync(guild=ctx.guild)
    await ctx.send("Slash commands synced for this server")

# check if bot is up 

@bot.listen('on_interaction')
async def log_interactions(interaction: discord.Interaction):
    cmd_name = interaction.data.get('name') if interaction.data else 'Unknown'
    print(f"[DEBUG] Received interaction: '{cmd_name}' from {interaction.user}")

# set logging channel

@bot.tree.command(name="setrolelog", description="Sets channel for timed role updates logging")
@app_commands.describe(channel="The channel to send timed role updates logs to")
@app_commands.checks.has_permissions(manage_guild=True)
async def setrolelog(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    await db_execute("""
            INSERT OR REPLACE INTO guild_settings (guild_id, role_log_channel_id)
            VALUES (?, ?)
    """, (str(interaction.guild.id), str(channel.id)))
    
    await interaction.followup.send(f"Timed role updates set up to be logged in {channel.mention}.", ephemeral=True)

# roles commands

@bot.tree.command(name="addrole", description="Assigns or extends a timed role for a member")
@app_commands.describe(
    user="User to assign role to",
    role="Role to be assigned",
    duration="Duration of role in days",
    amount="Sponsored amount"
)
@app_commands.checks.has_permissions(manage_roles=True)
async def addrole(
    interaction: discord.Interaction, 
    user: discord.User, 
    role: discord.Role, 
    duration: int | None = None,
    amount: int | None = None
):
    if duration is None and amount is None:
        return await interaction.response.send_message("Error: must fill in either `duration` or `amount` field.", ephemeral=True)
    elif duration is None:
        duration = round(amount * 92 / 10)
    await interaction.response.defer(ephemeral=True) # Give bot longer time to respond in interaction

    member = interaction.guild.get_member(user.id)
    action_type = "Assignment"

    # Check if user already has this role
    row = await db_fetch(
        "SELECT end_time FROM timed_roles WHERE user_id = ? AND guild_id = ? AND role_id = ?",
        (str(user.id), str(interaction.guild.id), str(role.id))
    )
    
    if row:
        # User already has role -> extend duration
        action_type = "Extension"
        new_end = datetime.fromisoformat(row[0]) + timedelta(days=duration)
        status_msg = f"Extended **{role.name}** for {user.mention} by {duration} days."
    else:
        # User does not have role yet
        new_end = datetime.now(timezone.utc) + timedelta(days=duration)
        status_msg = f"Assigned **{role.name}** to {user.mention} for {duration} days."
    
    # Assign role to user if they do not have it
    if member and role not in member.roles:
        try:
            await member.add_roles(role)
        except discord.Forbidden:
            return await interaction.followup.send(f"Error: {role.name} Role could not be added.", ephemeral=True)
    
    # Update database with new end timing
    await db_execute("""
        INSERT OR REPLACE INTO timed_roles (user_id, guild_id, role_id, end_time)
        VALUES (?, ?, ?, ?)
    """, (str(user.id), str(interaction.guild.id), str(role.id), new_end.isoformat()))

    # Send update log into specified channel
    row = await db_fetch(
        "SELECT role_log_channel_id FROM guild_settings WHERE guild_id = ?",
        (str(interaction.guild.id),)
    )

    if row:
        role_log_channel = bot.get_channel(int(row[0]))
        if role_log_channel:
            embed_color = discord.Color.green() if action_type == "Assignment" else discord.Color.blue() 

            embed = discord.Embed(
                title=f"Timed Role {action_type}",
                description=status_msg,
                color=embed_color,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=True)
            timestamp = int(new_end.timestamp())
            embed.add_field(name="New Role Expiration Date", value=f"<t:{timestamp}:F> (<t:{timestamp}:R>)")
            await role_log_channel.send(embed=embed)
        else:
            # Log channel not set up yet
            await interaction.followup.send("Warning: logging channel not set up", ephemeral=True)
    else:
        await interaction.followup.send("Warning: logging channel not set up", ephemeral=True)

    await interaction.followup.send("Role update successful!", ephemeral=True)

@bot.tree.command(name="checkrole", description="Check how much time you have left on a timed role")
@app_commands.describe(role="The timed role to check")
async def checkrole(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    row = await db_fetch("""
        SELECT end_time FROM timed_roles WHERE
        user_id = ? AND guild_id = ? AND role_id = ?
    """, (str(interaction.user.id), str(interaction.guild.id), str(role.id)))

    if not row:
        return await interaction.followup.send(
            f"You do not have an active timed tracker for the **{role.name}** role.", ephemeral=True
        )
    
    end_time = datetime.fromisoformat(row[0])
    unix_timestamp = int(end_time.timestamp())
    await interaction.followup.send(
        f"Your **{role.name}** role expires on <t:{unix_timestamp}:F> (<t:{unix_timestamp}:R>)", ephemeral=True
    )

# role expiry checker

async def check_role_expiry(guild_id: int = None):
    # Runs daily at 00:00 UTC to remove expired roles
    now = datetime.now(timezone.utc).isoformat()

    if guild_id:
        query = "SELECT user_id, guild_id, role_id FROM timed_roles WHERE end_time <= ? AND guild_id = ?"
        params = (now, str(guild_id))
    else:
        query = "SELECT user_id, guild_id, role_id FROM timed_roles WHERE end_time <= ?"
        params = (now,)
    expired_roles = await db_fetch(query, params, fetch_all=True)
    
    if not expired_roles:
        return
    
    for user_id, guild_id, role_id in expired_roles:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            # guild no longer exists, delete records
            await db_execute(
                "DELETE FROM timed_roles WHERE user_id = ? AND guild_id = ? AND role_id = ?",
                (str(user_id), str(guild_id), str(role_id)), commit=False
            )
            continue

        role = guild.get_role(int(role_id))
        member = guild.get_member(int(user_id))
        if not member:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.NotFound:
                member = None
        
        removed = False
        if member and role and role in member.roles:
            await member.remove_roles(role, reason="Timed role expired.")
            removed = True
        
        row = await db_fetch(
            "SELECT role_log_channel_id FROM guild_settings WHERE guild_id = ?",
            (str(guild.id),)
        )

        if row:
            role_log_channel = bot.get_channel(int(row[0]))
            if role_log_channel:
                embed = discord.Embed(
                    title=f"Timed Role Expiry",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                user_str = member.mention if member else f"`User ID: {user_id}`"
                role_str = role.mention if role else f"`Role ID: {role_id}`"
                embed.add_field(name="User", value=user_str, inline=True)
                embed.add_field(name="Role expired", value=role_str, inline=True)
                embed.add_field(
                    name="Status",
                    value="Role Removed" if removed else "Role already missing",
                    inline=False
                )
                await role_log_channel.send(embed=embed)

        await db_execute(
            "DELETE FROM timed_roles WHERE user_id = ? AND guild_id = ? AND role_id = ?",
            (str(user_id), str(guild_id), str(role_id)), commit=False
        )
    await db_commit()

CHECK_TIME = time(hour=0, minute=0, tzinfo=timezone.utc)

@tasks.loop(time=CHECK_TIME)
async def daily_expiry_check():
    try:
        await check_role_expiry()
        print(f"[INFO] Daily role expiry check completed at {datetime.now(timezone.utc).isoformat()}")
    except Exception as e:
        print(f"[ERROR] Error during daily role expiry check: {e}")

@daily_expiry_check.error
async def daily_expiry_check_error(error):
    print(f"[EXPIRY CHECK TASK ERROR]: {error}")

@bot.tree.command(name="checkexpiry", description="Immediately run role expiration check for this server.")
@app_commands.checks.has_permissions(administrator=True)
async def checkexpiry(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await check_role_expiry(guild_id=interaction.guild.id)
    await interaction.followup.send("Role expiry check completed for this server.", ephemeral=True)

# view roles

class RolePageView(discord.ui.View):
    def __init__(self, data, role_name, per_page=10):
        super().__init__(timeout=60.0)
        self.data = data
        self.role_name = role_name
        self.per_page = per_page
        self.current_page = 0
        self.max_pages = max(1, (len(data)-1) // per_page + 1) if data else 1
        self.update_buttons()
    
    def update_buttons(self):
        self.children[0].disabled = self.current_page == 0
        self.children[1].disabled = self.current_page >= self.max_pages - 1
    
    def make_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_items = self.data[start:end]

        embed = discord.Embed(
            title=f"{self.role_name} Role Members",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone(timedelta(0)))
        )

        description_lines = []
        for user_id, end_time_str in page_items:
            end_time = datetime.fromisoformat(end_time_str)
            unix_ts = int(end_time.timestamp())
            description_lines.append(f"<@{user_id}>: Expires at <t:{unix_ts}:F>")
        embed.description = "\n".join(description_lines) if description_lines else "No members with role."
        embed.set_footer(text=f"Page {self.current_page + 1} of {self.max_pages} | Total Records: {len(self.data)}")
        return embed
    
    @discord.ui.button(label="< Previous", style=discord.ButtonStyle.grey, custom_id="prev_page")
    async def prev_page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)
    
    @discord.ui.button(label="Next >", style=discord.ButtonStyle.grey, custom_id="next_page")
    async def next_page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

@bot.tree.command(name="listrole", description="List all members with a specific timed role")
@app_commands.describe(role="The timed role to check")
@app_commands.checks.has_permissions(manage_roles=True)
async def listrole(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=False)
    rows = await db_fetch("""
        SELECT user_id, end_time FROM timed_roles
        WHERE guild_id = ? AND role_id = ?
        ORDER BY end_time ASC
    """, (str(interaction.guild.id), str(role.id)), fetch_all=True)
    if not rows:
        return await interaction.followup.send(f"No users with **{role.name}** role.", ephemeral=True)
    view = RolePageView(data=rows, role_name=role.name)
    await interaction.followup.send(embed=view.make_embed(), view=view, ephemeral=False)

# clear roles and records for certain server
@bot.tree.command(name="clearrole", description="Clear and remove a certain timed role in server.")
@app_commands.describe(role="The timed role to clear")
@app_commands.checks.has_permissions(administrator=True)
async def clearrole(interaction: discord.Interaction, role: discord.Role = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    if role is not None: # clear certain role only
        rows = await db_fetch(
            "SELECT user_id FROM timed_roles WHERE guild_id = ? AND role_id = ?",
            (str(guild.id), str(role.id)), fetch_all=True
        )
        if not rows and not role.members:
            return await interaction.followup.send(f"No active timed roles for **{role.name}**.", ephemeral=True)
        
        await db_execute(
            "DELETE FROM timed_roles WHERE guild_id = ? AND role_id = ?",
            (str(guild.id), str(role.id))
        )

        removed_count = 0
        for member in role.members:
            await member.remove_roles(role)
            removed_count += 1
        await interaction.followup.send(f"Cleared **{role.name}** role.", ephemeral=True)
        log_title = "Timed Role Cleared"
        log_description = f"{interaction.user.mention} cleared the **{role.name}** role."
        db_count = len(rows)
        dc_count = removed_count
    
    else: # Clear all timed roles in server
        rows = await db_fetch(
            "SELECT user_id, role_id FROM timed_roles WHERE guild_id = ?",
            (str(guild.id),), fetch_all=True
        )
        if not rows:
            return await interaction.followup.send("No active timed roles in this server.", ephemeral=True)
        
        removed_count = 0
        for user_id, role_id in rows:
            member = guild.get_member(user_id)
            target_role = guild.get_role(role_id)
            if member and target_role and target_role in member.roles:
                await member.remove_roles(target_role)
                removed_count += 1
        
        await db_execute("DELETE FROM timed_roles WHERE guild_id = ?", (str(guild.id),))
        await interaction.followup.send(f"Cleared all timed roles in server.", ephemeral=True)

        log_title = "Timed Roles Cleared"
        log_description = f"{interaction.user.mention} cleared **all** timed roles for this server."
        db_count = len(rows)
        dc_count = removed_count
        
    row = await db_fetch(
        "SELECT role_log_channel_id FROM guild_settings WHERE guild_id = ?", (str(guild.id),)
    )

    if row:
        role_log_channel = bot.get_channel(int(row[0]))
        if role_log_channel:
            embed = discord.Embed(
                title=log_title,
                description=log_description,
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc)
            )
            if role:
                embed.add_field(name="Role cleared", value=role.mention, inline=True)
            else:
                embed.add_field(name="Role cleared", value="all", inline=True)
            embed.add_field(name="Database records removed", value=str(db_count), inline=True)
            embed.add_field(name="Roles removed", value=str(dc_count), inline=True)
            await role_log_channel.send(embed=embed)
        else:
            # Log channel not set up yet
            await interaction.followup.send("Warning: logging channel not set up", ephemeral=True)
    else:
        await interaction.followup.send("Warning: logging channel not set up", ephemeral=True)

# run bot

def main():
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)

if __name__ == "__main__":
    main()
