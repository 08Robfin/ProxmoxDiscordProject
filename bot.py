import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv
import urllib3
import asyncio
from datetime import datetime, timezone, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
PROXMOX_HOST = os.getenv('PROXMOX_HOST')
PROXMOX_USER = os.getenv('PROXMOX_USER')
PROXMOX_TOKEN = os.getenv('PROXMOX_TOKEN')
PROXMOX_NODE = 'pve'

CATEGORY_ONLINE = "🟢 Online"
CATEGORY_OFFLINE = "🔴 Offline"

proxmox = ProxmoxAPI(
    PROXMOX_HOST,
    user=PROXMOX_USER.split('!')[0],
    token_name=PROXMOX_USER.split('!')[1],
    token_value=PROXMOX_TOKEN,
    verify_ssl=False
)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# { channel_id: message_id } — the live status embed per channel
status_messages = {}

# { channel_id: "running" | "stopped" } — avoid redundant category moves
last_known_state = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_vm_status(m_type, m_id):
    if m_type == 'qemu':
        return proxmox.nodes(PROXMOX_NODE).qemu(m_id).status.current.get()
    else:
        return proxmox.nodes(PROXMOX_NODE).lxc(m_id).status.current.get()


def build_status_embed(name, m_type, status_data, next_update_ts):
    state = status_data.get('status', 'unknown')
    is_online = state == 'running'

    embed = discord.Embed(
        title=f"{'🟢' if is_online else '🔴'} {name}",
        color=discord.Color.green() if is_online else discord.Color.red()
    )
    embed.add_field(name="Type", value=m_type.upper(), inline=True)
    embed.add_field(name="Status", value=state.capitalize(), inline=True)

    if is_online:
        cpu_usage = status_data.get('cpu', 0) * 100
        cpus = status_data.get('cpus', 'N/A')
        max_mem = status_data.get('maxmem', 0) / (1024 ** 3)
        used_mem = status_data.get('mem', 0) / (1024 ** 3)
        uptime_s = status_data.get('uptime', 0)
        uptime_str = f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m {uptime_s % 60}s"

        embed.add_field(name="CPU Cores", value=str(cpus), inline=True)
        embed.add_field(name="CPU Usage", value=f"{cpu_usage:.2f}%", inline=True)
        embed.add_field(name="RAM Used", value=f"{used_mem:.2f} / {max_mem:.2f} GB", inline=True)
        embed.add_field(name="Uptime", value=uptime_str, inline=True)

    embed.description = f"-# Last refreshed <t:{next_update_ts}:R>"
    embed.set_footer(text="Proxmox Live Monitor")
    return embed, state


async def get_or_create_category(guild, name):
    """Find an existing category by name or create it."""
    cat = discord.utils.get(guild.categories, name=name)
    if not cat:
        cat = await guild.create_category(name)
    return cat


async def move_to_category(channel, guild, state):
    """Move channel to Online or Offline category, only if state changed."""
    prev = last_known_state.get(channel.id)
    if prev == state:
        return  # No change, don't touch it

    target_name = CATEGORY_ONLINE if state == "running" else CATEGORY_OFFLINE
    target_cat = await get_or_create_category(guild, target_name)

    if channel.category_id != target_cat.id:
        try:
            await channel.edit(category=target_cat)
        except discord.HTTPException:
            return

    last_known_state[channel.id] = state


async def find_existing_status_message(channel):
    """
    On restart, scan pinned messages to find the bot's old status embed
    so we reuse it instead of posting a duplicate.
    """
    try:
        pins = await channel.pins()
        for msg in pins:
            if msg.author == bot.user and msg.embeds:
                embed = msg.embeds[0]
                if embed.footer and "Proxmox Live Monitor" in embed.footer.text:
                    return msg
    except discord.HTTPException:
        pass
    return None


async def post_or_edit_status(channel, name, m_type, m_id, next_update_ts=None):
    """Edit the existing status embed, or find/post one if needed."""
    try:
        status_data = get_vm_status(m_type, m_id)
    except Exception:
        return

    if next_update_ts is None:
        next_update_ts = int(datetime.now(timezone.utc).timestamp())
    embed, state = build_status_embed(name, m_type, status_data, next_update_ts)

    # Move to correct category if state changed
    await move_to_category(channel, channel.guild, state)

    msg_id = status_messages.get(channel.id)

    # Try editing the in-memory cached message
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            status_messages.pop(channel.id, None)

    # Bot restarted — scan pins before sending a new message
    existing = await find_existing_status_message(channel)
    if existing:
        status_messages[channel.id] = existing.id
        await existing.edit(embed=embed)
        return

    # Nothing found, send a fresh pinned message
    msg = await channel.send(embed=embed)
    await msg.pin()
    status_messages[channel.id] = msg.id


# ── Background task ────────────────────────────────────────────────────────────

@tasks.loop(seconds=30)
async def live_monitor():
    for guild in bot.guilds:
        # Ensure both categories exist
        cat_online = await get_or_create_category(guild, CATEGORY_ONLINE)
        cat_offline = await get_or_create_category(guild, CATEGORY_OFFLINE)

        # Grab all VMs from Proxmox
        try:
            qemu_vms = proxmox.nodes(PROXMOX_NODE).qemu.get()
            lxc_vms = proxmox.nodes(PROXMOX_NODE).lxc.get()
        except Exception as e:
            print(f"Proxmox unreachable during monitor: {e}")
            continue

        pve_machines = {}
        for vm in qemu_vms:
            pve_machines[f"qemu:{vm['vmid']}"] = vm.get('name', f"vm-{vm['vmid']}")
        for ct in lxc_vms:
            pve_machines[f"lxc:{ct['vmid']}"] = ct.get('name', f"ct-{ct['vmid']}")

        # Build a map of existing Proxmox channels
        existing_topics = {}
        for channel in guild.text_channels:
            topic = channel.topic
            if topic and ":" in topic and (topic.startswith("qemu:") or topic.startswith("lxc:")):
                existing_topics[topic] = channel

        # Delete channels for VMs that no longer exist
        for topic, channel in list(existing_topics.items()):
            if topic not in pve_machines:
                await channel.delete(reason="VM no longer in Proxmox")
                await asyncio.sleep(1)

        # Calculate next update timestamp once for the whole cycle
        next_update_ts = int(datetime.now(timezone.utc).timestamp())

        # Update existing channels and create missing ones
        for topic, name in pve_machines.items():
            m_type, m_id = topic.split(":", 1)
            if topic in existing_topics:
                # Channel exists — just update the embed and category
                await post_or_edit_status(existing_topics[topic], name, m_type, m_id, next_update_ts)
            else:
                # Channel missing — create it
                try:
                    status_data = get_vm_status(m_type, m_id)
                    state = status_data.get('status', 'stopped')
                except Exception:
                    state = 'stopped'
                target_cat = cat_online if state == "running" else cat_offline
                channel = await guild.create_text_channel(name, topic=topic, category=target_cat)
                last_known_state[channel.id] = state
                await post_or_edit_status(channel, name, m_type, m_id)
                await asyncio.sleep(2)

            await asyncio.sleep(1)


# ── Bot events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Proxmox"
        )
    )
    live_monitor.start()


# ── Commands ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="sync", description="Smart sync: creates/deletes channels, moves to correct category")
async def sync(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild

    # Ensure both categories exist upfront
    cat_online = await get_or_create_category(guild, CATEGORY_ONLINE)
    cat_offline = await get_or_create_category(guild, CATEGORY_OFFLINE)

    # Grab all VMs from Proxmox
    qemu_vms = proxmox.nodes(PROXMOX_NODE).qemu.get()
    lxc_vms = proxmox.nodes(PROXMOX_NODE).lxc.get()

    pve_machines = {}
    for vm in qemu_vms:
        pve_machines[f"qemu:{vm['vmid']}"] = vm.get('name', f"vm-{vm['vmid']}")
    for ct in lxc_vms:
        pve_machines[f"lxc:{ct['vmid']}"] = ct.get('name', f"ct-{ct['vmid']}")

    # Check existing channels — delete dead ones, update live ones
    for channel in guild.text_channels:
        topic = channel.topic
        if topic and (topic.startswith("qemu:") or topic.startswith("lxc:")):
            if topic not in pve_machines:
                await channel.delete(reason="VM no longer in Proxmox")
                await asyncio.sleep(1)
            else:
                m_type, m_id = topic.split(":", 1)
                name = pve_machines[topic]
                await post_or_edit_status(channel, name, m_type, m_id)
                del pve_machines[topic]
                await asyncio.sleep(1)

    # Create channels for brand new VMs
    for topic, name in pve_machines.items():
        m_type, m_id = topic.split(":", 1)
        try:
            status_data = get_vm_status(m_type, m_id)
            state = status_data.get('status', 'stopped')
        except Exception:
            state = 'stopped'

        target_cat = cat_online if state == "running" else cat_offline
        channel = await guild.create_text_channel(name, topic=topic, category=target_cat)
        last_known_state[channel.id] = state
        await post_or_edit_status(channel, name, m_type, m_id)
        await asyncio.sleep(2)

    await interaction.followup.send("Smart Sync Complete! ✅", delete_after=30)


@bot.tree.command(name="start", description="Starts the machine")
async def start(interaction: discord.Interaction):
    topic_data = interaction.channel.topic
    if not topic_data or ":" not in topic_data:
        return await interaction.response.send_message("This isn't a valid Proxmox channel!", ephemeral=True)

    m_type, m_id = topic_data.split(':', 1)

    if m_type == 'qemu':
        proxmox.nodes(PROXMOX_NODE).qemu(m_id).status.start.post()
    else:
        proxmox.nodes(PROXMOX_NODE).lxc(m_id).status.start.post()

    await interaction.response.send_message("Machine is booting up! 🟢 Status will update shortly.", delete_after=30)


@bot.tree.command(name="stop", description="Stops the machine")
async def stop(interaction: discord.Interaction):
    topic_data = interaction.channel.topic
    if not topic_data or ":" not in topic_data:
        return await interaction.response.send_message("This isn't a valid Proxmox channel!", ephemeral=True)

    m_type, m_id = topic_data.split(':', 1)

    if m_type == 'qemu':
        proxmox.nodes(PROXMOX_NODE).qemu(m_id).status.stop.post()
    else:
        proxmox.nodes(PROXMOX_NODE).lxc(m_id).status.stop.post()

    await interaction.response.send_message("Machine is shutting down! 🔴 Status will update shortly.", delete_after=30)


bot.run(DISCORD_TOKEN)