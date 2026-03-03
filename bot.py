import os
import discord
from discord.ext import commands
from discord import app_commands
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv
import urllib3
import asyncio 

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
PROXMOX_HOST = os.getenv('PROXMOX_HOST')
PROXMOX_USER = os.getenv('PROXMOX_USER')
PROXMOX_TOKEN = os.getenv('PROXMOX_TOKEN')
PROXMOX_NODE = 'pve'

proxmox = ProxmoxAPI(
    PROXMOX_HOST, 
    user=PROXMOX_USER.split('!')[0], 
    token_name=PROXMOX_USER.split('!')[1], 
    token_value=PROXMOX_TOKEN, 
    verify_ssl=False
)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

@bot.tree.command(name="sync", description="Smart sync: Creates new channels, deletes dead ones")
async def sync(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    
    # 1. Grab everything from Proxmox
    qemu_vms = proxmox.nodes(PROXMOX_NODE).qemu.get()
    lxc_vms = proxmox.nodes(PROXMOX_NODE).lxc.get()
    
    # Put them in a "Dictionary" (A map of IDs to Names)
    pve_machines = {}
    for vm in qemu_vms:
        pve_machines[f"qemu:{vm['vmid']}"] = vm.get('name', f"vm-{vm['vmid']}")
    for ct in lxc_vms:
        pve_machines[f"lxc:{ct['vmid']}"] = ct.get('name', f"ct-{ct['vmid']}")
        
    # 2. Check existing Discord channels by reading their topic
    for channel in guild.text_channels:
        topic = channel.topic
        if topic and (topic.startswith("qemu:") or topic.startswith("lxc:")):
            if topic not in pve_machines:
                # The VM was deleted from Proxmox, so trash the channel
                await channel.delete(reason="VM no longer in Proxmox")
                await asyncio.sleep(1) # Anti-ban delay
            else:
                # It exists in both! Remove it from our to-do list to prevent duplicates
                del pve_machines[topic]
                
    # 3. Create channels for the NEW stuff left in our to-do list
    for topic, name in pve_machines.items():
        channel_name = f"🔴-{name}"
        channel = await guild.create_text_channel(channel_name, topic=topic)
        await channel.send(f"Channel created for: **{name}**")
        await asyncio.sleep(2) # Anti-ban delay
        
    await interaction.followup.send("Smart Sync Complete! ✅ Old channels deleted, new ones added.")

@bot.tree.command(name="start", description="Starts the machine")
async def start(interaction: discord.Interaction):
    topic_data = interaction.channel.topic 
    if not topic_data or ":" not in topic_data:
        return await interaction.response.send_message("This isn't a valid Proxmox channel!")
        
    m_type, m_id = topic_data.split(':')
    
    if m_type == 'qemu':
        proxmox.nodes(PROXMOX_NODE).qemu(m_id).status.start.post()
    else:
        proxmox.nodes(PROXMOX_NODE).lxc(m_id).status.start.post()
        
    # Smart rename logic: Splits at the first dash and keeps the name part
    clean_name = interaction.channel.name.split('-', 1)[-1]
    await interaction.channel.edit(name=f"🟢-{clean_name}")
    
    await interaction.response.send_message("Machine is booting up! 🟢 Channel name updated.")

@bot.tree.command(name="stop", description="Stops the machine")
async def stop(interaction: discord.Interaction):
    topic_data = interaction.channel.topic 
    if not topic_data or ":" not in topic_data:
        return await interaction.response.send_message("This isn't a valid Proxmox channel!")
        
    m_type, m_id = topic_data.split(':')
    
    if m_type == 'qemu':
        proxmox.nodes(PROXMOX_NODE).qemu(m_id).status.stop.post()
    else:
        proxmox.nodes(PROXMOX_NODE).lxc(m_id).status.stop.post()
        
    # Smart rename logic
    clean_name = interaction.channel.name.split('-', 1)[-1]
    await interaction.channel.edit(name=f"🔴-{clean_name}")
        
    await interaction.response.send_message("Machine is shutting down! 🔴 Channel name updated.")

@bot.tree.command(name="status", description="Gets live stats")
async def status(interaction: discord.Interaction):
    topic_data = interaction.channel.topic 
    if not topic_data or ":" not in topic_data:
        return await interaction.response.send_message("This isn't a valid Proxmox channel!")
        
    await interaction.response.defer()
    m_type, m_id = topic_data.split(':')
    
    if m_type == 'qemu':
        status_data = proxmox.nodes(PROXMOX_NODE).qemu(m_id).status.current.get()
    else:
        status_data = proxmox.nodes(PROXMOX_NODE).lxc(m_id).status.current.get()
    
    state = status_data.get('status', 'unknown')
    max_mem = status_data.get('maxmem', 0) / (1024**3) 
    cpu_usage = status_data.get('cpu', 0) * 100 
    cpus = status_data.get('cpus', 0)
    
    msg = f"**Type:** {m_type.upper()}\n"
    msg += f"**Status:** {state}\n"
    msg += f"**CPU Cores:** {cpus}\n"
    msg += f"**CPU Usage:** {cpu_usage:.2f}%\n"
    msg += f"**Max RAM:** {max_mem:.2f} GB"
    
    await interaction.followup.send(msg)

bot.run(DISCORD_TOKEN)