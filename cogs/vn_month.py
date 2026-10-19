"""
VN of the Month feature cog
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

from utils import (
    VNDBClient, JitenClient, Database, MonthlyBannerGenerator,
    format_date, format_rating, format_vn_length,
    truncate_text, convert_bbcode_to_markdown, extract_vndb_id, validate_month_year, get_month_name,
    is_content_nsfw, get_safe_image_url, parse_completion_date, calculate_vn_score,
    days_in_month, chars_per_day,
)

logger = logging.getLogger(__name__)

# Rate limiting for completion commands (user_id -> last_completion_time)
completion_rate_limit = defaultdict(datetime)

class PoolNavigationView(discord.ui.View):
    """Navigation view for VN pool pagination by month or pages"""
    
    def __init__(self, month: int, year: int, filter_type: str = None):
        super().__init__(timeout=300)  # 5 minute timeout
        self.month = month
        self.year = year
        self.filter_type = filter_type  # Store the filter type
        self.is_all_view = False
        self.current_page = 1
        self.total_pages = 1
    
    def _get_previous_month(self):
        """Get the previous month/year"""
        if self.month == 1:
            return 12, self.year - 1
        return self.month - 1, self.year
    
    def _get_next_month(self):
        """Get the next month/year"""
        if self.month == 12:
            return 1, self.year + 1
        return self.month + 1, self.year
    
    async def _update_embed(self, interaction: discord.Interaction, month: int, year: int, show_all: bool = False, page: int = 1):
        """Update the embed with new month/year data or paginated all VNs"""
        db = Database()
        await db.initialize()
        
        try:
            if show_all:
                # Get all VNs regardless of target month for pagination
                items_per_page = 10
                offset = (page - 1) * items_per_page
                
                # Get total count for pagination
                all_pool_vns = await db.get_vn_pool(None, None, 1000)  # Get all to count
                
                # Apply the filter if one is set
                if self.filter_type == "monthly":
                    all_pool_vns = [vn for vn in all_pool_vns if vn.get('status') == 'vn_of_month']
                elif self.filter_type == "pool":
                    all_pool_vns = [vn for vn in all_pool_vns if vn.get('status') != 'vn_of_month']
                # If filter_type is None, show all VNs
                
                total_vns = len(all_pool_vns)
                self.total_pages = max(1, (total_vns + items_per_page - 1) // items_per_page)
                
                # Get VNs for current page
                pool_vns = all_pool_vns[offset:offset + items_per_page]
                
                title_suffix = f"All VNs (Page {page}/{self.total_pages})"
                self.current_page = page
                self.is_all_view = True
            else:
                # Get VNs for the new month/year
                pool_vns = await db.get_vn_pool(month, year, 25)
                
                # Apply the filter if one is set
                if self.filter_type == "monthly":
                    pool_vns = [vn for vn in pool_vns if vn.get('status') == 'vn_of_month']
                elif self.filter_type == "pool":
                    pool_vns = [vn for vn in pool_vns if vn.get('status') != 'vn_of_month']
                # If filter_type is None, show all VNs
                
                month_name = get_month_name(month)
                title_suffix = f"**{month_name} {year}**"
                self.is_all_view = False
            
            # Create new embed
            embed = discord.Embed(
                title="VN Pool",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            
            # Build description based on filter and view type
            if show_all:
                if self.filter_type == "monthly":
                    embed.description = f"**VN of the Month** entries - {title_suffix}"
                elif self.filter_type == "pool":
                    embed.description = f"Suggested VNs (non-monthly) - {title_suffix}"
                else:
                    embed.description = f"VNs targeted for {title_suffix}"
            else:
                month_name = get_month_name(month)
                if self.filter_type == "monthly":
                    embed.description = f"**VN of the Month** entries for **{month_name} {year}**"
                elif self.filter_type == "pool":
                    embed.description = f"Suggested VNs (non-monthly) for **{month_name} {year}**"
                else:
                    embed.description = f"VNs targeted for **{month_name} {year}**"
            
            if not pool_vns:
                if show_all:
                    if self.filter_type == "monthly":
                        no_vns_message = "No VN of the Month entries found in the pool"
                    elif self.filter_type == "pool":
                        no_vns_message = "No suggested VNs (non-monthly) found in the pool"
                    else:
                        no_vns_message = "The VN pool is empty"
                    
                    embed.add_field(
                        name="No VNs Found",
                        value=no_vns_message,
                        inline=False
                    )
                else:
                    month_name = get_month_name(month)
                    if self.filter_type == "monthly":
                        no_vns_message = f"No VN of the Month entries found for {month_name} {year}"
                    elif self.filter_type == "pool":
                        no_vns_message = f"No suggested VNs (non-monthly) found for {month_name} {year}"
                    else:
                        no_vns_message = f"No VNs are targeted for {month_name} {year}"
                    
                    embed.add_field(
                        name="No VNs Found",
                        value=no_vns_message,
                        inline=False
                    )
            else:
                # Show VNs
                display_count = min(20, len(pool_vns))
                for i, vn in enumerate(pool_vns[:display_count]):
                    vn_text = f"**ID:** {vn['id']} • [VNDB](https://vndb.org/{vn['vndb_id']})"
                    
                    # Check if this VN is currently VN of the month using status field
                    if vn.get('status') == 'vn_of_month':
                        if show_all and vn.get('target_month') and vn.get('target_year'):
                            month_name_status = get_month_name(vn['target_month'])
                            vn_text += f"\n👑 **VN of {month_name_status} {vn['target_year']}**"
                        else:
                            vn_text += f"\n👑 **VN of the Month**"
                    
                    if vn.get('rating'):
                        vn_text += f" • {format_rating(vn['rating'])}"
                    if vn.get('added_by_name'):
                        vn_text += f"\n*Suggested by {vn['added_by_name']}*"
                    
                    # Show target month for "All VNs" view
                    if show_all and vn.get('target_month') and vn.get('target_year'):
                        target_month_name = get_month_name(vn['target_month'])
                        vn_text += f"\n📅 *Target: {target_month_name} {vn['target_year']}*"
                    
                    # Calculate score for this VN
                    is_monthly = vn.get('status') == 'vn_of_month'
                    score = calculate_vn_score(
                        length=vn.get('length'),
                        is_vn_of_month=is_monthly,
                        completed_in_target_month=False
                    )
                    
                    # Create title with number and score
                    title_text = f"{i+1}. {vn['title'][:40]} ({score} points)"
                    
                    embed.add_field(
                        name=title_text,
                        value=vn_text,
                        inline=True
                    )
                
                # Update footer based on view type
                if show_all:
                    if self.total_pages > 1:
                        start_num = (page - 1) * 20 + 1
                        end_num = min(page * 20, total_vns)
                        embed.set_footer(text=f"VNCR • Showing {start_num}-{end_num} of {total_vns} entries (Page {page}/{self.total_pages})")
                    else:
                        embed.set_footer(text=f"VNCR • {total_vns} entries (All VNs)")
                else:
                    month_name = get_month_name(month)
                    if len(pool_vns) > 20:
                        embed.set_footer(text=f"VNCR • Showing 20 of {len(pool_vns)} entries for {month_name} {year}")
                    else:
                        embed.set_footer(text=f"VNCR • {len(pool_vns)} entries for {month_name} {year}")
            
            # Update the view's month/year
            if not show_all:
                self.month = month
                self.year = year
            
            # Update button states
            self._update_button_states()
            
            return embed
            
        except Exception as e:
            logger.error(f"Error updating pool embed: {e}")
            embed = discord.Embed(
                title="Error",
                description="An error occurred while retrieving VN pool data.",
                color=discord.Color.red()
            )
            return embed
    
    def _update_button_states(self):
        """Update button labels and disabled states based on current view mode"""
        if self.is_all_view:
            # Update buttons for page navigation
            self.children[0].label = '◀ Previous Page'
            self.children[0].disabled = (self.current_page <= 1)
            
            self.children[1].label = 'Month View'
            self.children[1].disabled = False
            
            self.children[2].label = 'Next Page ▶'
            self.children[2].disabled = (self.current_page >= self.total_pages)
            
            self.children[3].label = f'📋 Page {self.current_page}/{self.total_pages}'
            self.children[3].disabled = True
        else:
            # Update buttons for month navigation
            self.children[0].label = '◀ Previous Month'
            self.children[0].disabled = False
            
            self.children[1].label = 'Current Month'
            self.children[1].disabled = False
            
            self.children[2].label = 'Next Month ▶'
            self.children[2].disabled = False
            
            self.children[3].label = '📋 All VNs'
            self.children[3].disabled = False
    
    @discord.ui.button(label='◀ Previous Month', style=discord.ButtonStyle.secondary)
    async def previous_month_or_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_all_view:
            # Previous page
            if self.current_page > 1:
                embed = await self._update_embed(interaction, self.month, self.year, show_all=True, page=self.current_page - 1)
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.response.defer()
        else:
            # Previous month
            prev_month, prev_year = self._get_previous_month()
            embed = await self._update_embed(interaction, prev_month, prev_year)
            await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label='Current Month', style=discord.ButtonStyle.primary)
    async def current_month_or_month_view(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_all_view:
            # Switch back to month view
            current_date = datetime.now()
            embed = await self._update_embed(interaction, current_date.month, current_date.year)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            # Go to current month
            current_date = datetime.now()
            embed = await self._update_embed(interaction, current_date.month, current_date.year)
            await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label='Next Month ▶', style=discord.ButtonStyle.secondary)
    async def next_month_or_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_all_view:
            # Next page
            if self.current_page < self.total_pages:
                embed = await self._update_embed(interaction, self.month, self.year, show_all=True, page=self.current_page + 1)
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.response.defer()
        else:
            # Next month
            next_month, next_year = self._get_next_month()
            embed = await self._update_embed(interaction, next_month, next_year)
            await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label='📋 All VNs', style=discord.ButtonStyle.success)
    async def all_vns_or_page_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_all_view:
            # This button shows page info when in all view mode, so just defer
            await interaction.response.defer()
        else:
            # Switch to all VNs view
            embed = await self._update_embed(interaction, self.month, self.year, show_all=True, page=1)
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def on_timeout(self):
        """Called when the view times out"""
        # Disable all buttons
        for item in self.children:
            item.disabled = True

class CompletionsNavigationView(discord.ui.View):
    """Navigation view for user completions pagination"""
    
    def __init__(self, user_id: int, target_user: discord.Member):
        super().__init__(timeout=300)  # 5 minute timeout
        self.user_id = user_id
        self.target_user = target_user
        self.current_page = 1
        self.total_pages = 1
        self.items_per_page = 10
    
    async def _update_embed(self, interaction: discord.Interaction, page: int):
        """Update the embed with new page data"""
        db = Database()
        await db.initialize()
        
        try:
            # Get all completions for pagination calculation
            all_completions = await db.get_user_completions(self.target_user.id, limit=1000)
            total_completions = len(all_completions)
            self.total_pages = max(1, (total_completions + self.items_per_page - 1) // self.items_per_page)
            
            # Get completions for current page
            offset = (page - 1) * self.items_per_page
            page_completions = all_completions[offset:offset + self.items_per_page]
            
            # Calculate statistics from actual completions (consistent with display)
            total_score = 0
            vn_of_month_count = 0
            ratings = []
            
            for completion in all_completions:
                # Collect ratings for average calculation
                if completion.get('rating'):
                    ratings.append(completion['rating'])
                
                # Count VN of the Month completions
                if completion.get('status') == 'vn_of_month':
                    vn_of_month_count += 1
                
                # Calculate score for total
                # Check if completed in target month for bonus
                completed_in_target_month = False
                if (completion.get('target_month') and completion.get('target_year') and 
                    completion.get('completed_at')):
                    # Parse completion date (YYYY-MM-DD format)
                    completed_date_parts = completion['completed_at'][:10].split('-')
                    if len(completed_date_parts) >= 3:
                        try:
                            completed_year = int(completed_date_parts[0])
                            completed_month = int(completed_date_parts[1])
                            target_month = int(completion['target_month'])
                            target_year = int(completion['target_year'])
                            
                            completed_in_target_month = (completed_month == target_month and 
                                                       completed_year == target_year)
                        except (ValueError, TypeError):
                            pass
                
                # Check if this was VN of the month when completed
                # The status field comes directly from the database
                is_monthly = completion.get('status') == 'vn_of_month'
                
                score = calculate_vn_score(
                    length=completion.get('length'),
                    is_vn_of_month=is_monthly,
                    completed_in_target_month=completed_in_target_month
                )
                total_score += score
            
            # Calculate average rating
            average_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
            
            # Create embed
            embed = discord.Embed(
                title=f"📚 {self.target_user.display_name}'s Completed VNs",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            # Add stats
            stats_text = f"**Total Completed:** {total_completions}\n"
            if average_rating:
                stats_text += f"**Average Rating:** {average_rating}/10\n"
            stats_text += f"**VN of the Month Completed:** {vn_of_month_count}\n"
            stats_text += f"**Total Score:** {total_score} points"
            
            embed.add_field(name="📊 Statistics", value=stats_text, inline=False)
            
            if not page_completions:
                if page == 1:
                    embed.add_field(
                        name="No Completions",
                        value=f"{self.target_user.display_name} hasn't completed any VNs yet.",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="No More Completions",
                        value="No completions found on this page.",
                        inline=False
                    )
            else:
                # Show completions for this page
                for i, completion in enumerate(page_completions):
                    # Format completion info (no redundant title)
                    vn_text = f"**Pool ID:** {completion['pool_id']} • [VNDB](https://vndb.org/{completion['vndb_id']})"
                    # Show VNDB rating on the same line if available
                    if completion.get('vndb_rating'):
                        vn_text += f" • {format_rating(completion['vndb_rating'])}"
                    # Show user rating on new line if available
                    if completion.get('rating'):
                        vn_text += f"\n⭐ **Your Rating:** {completion['rating']}/10"
                    
                    # Show target month
                    if completion.get('target_month') and completion.get('target_year'):
                        month_name = get_month_name(completion['target_month'])
                        vn_text += f"\n📅 *Target: {month_name} {completion['target_year']}*"
                    
                    # Show completion date
                    completed_date = completion['completed_at'][:10]  # Just the date part
                    vn_text += f"\n✅ *Completed: {completed_date}*"
                    
                    # Show review if available (truncated)
                    if completion.get('review'):
                        vn_text += f"\n💭 *\"{truncate_text(completion['review'], 100)}\"*"
                    
                    # Calculate score for this completion
                    # Check if completed in target month for bonus
                    completed_in_target_month = False
                    if (completion.get('target_month') and completion.get('target_year') and 
                        completion.get('completed_at')):
                        # Parse completion date (YYYY-MM-DD format)
                        completed_date_parts = completion['completed_at'][:10].split('-')
                        if len(completed_date_parts) >= 3:
                            try:
                                completed_year = int(completed_date_parts[0])
                                completed_month = int(completed_date_parts[1])
                                target_month = int(completion['target_month'])
                                target_year = int(completion['target_year'])
                                
                                completed_in_target_month = (completed_month == target_month and 
                                                           completed_year == target_year)
                            except (ValueError, TypeError):
                                pass
                    
                    # Check if this was VN of the month when completed
                    # The status field comes directly from the database
                    is_monthly = completion.get('status') == 'vn_of_month'
                    
                    score = calculate_vn_score(
                        length=completion.get('length'),
                        is_vn_of_month=is_monthly,
                        completed_in_target_month=completed_in_target_month
                    )
                    
                    # Create title with number and score (without length)
                    title_number = offset + i + 1
                    title_text = f"{title_number}. {completion['title'][:40]} ({score} points)"
                    
                    embed.add_field(
                        name=title_text,
                        value=vn_text,
                        inline=True
                    )
            
            # Update pagination info
            self.current_page = page
            self._update_button_states()
            
            # Footer with pagination info
            if self.total_pages > 1:
                start_num = offset + 1
                end_num = min(offset + self.items_per_page, total_completions)
                embed.set_footer(text=f"VNCR • Showing {start_num}-{end_num} of {total_completions} completions (Page {page}/{self.total_pages})")
            else:
                embed.set_footer(text=f"VNCR • {total_completions} completed VNs")
            
            return embed
            
        except Exception as e:
            logger.error(f"Error updating completions embed: {e}")
            embed = discord.Embed(
                title="Error",
                description="An error occurred while retrieving completion data.",
                color=discord.Color.red()
            )
            return embed
    
    def _update_button_states(self):
        """Update button labels and disabled states"""
        self.children[0].disabled = (self.current_page <= 1)
        self.children[2].disabled = (self.current_page >= self.total_pages)
        self.children[1].label = f'📋 Page {self.current_page}/{self.total_pages}'
    
    @discord.ui.button(label='◀ Previous Page', style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            embed = await self._update_embed(interaction, self.current_page - 1)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()
    
    @discord.ui.button(label='📋 Page 1/1', style=discord.ButtonStyle.primary, disabled=True)
    async def page_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        # This button just shows page info, so defer the interaction
        await interaction.response.defer()
    
    @discord.ui.button(label='Next Page ▶', style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages:
            embed = await self._update_embed(interaction, self.current_page + 1)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()
    
    async def on_timeout(self):
        """Called when the view times out"""
        # Disable all buttons
        for item in self.children:
            item.disabled = True

class VNHelpView(discord.ui.View):
    """Pagination view for VN help commands"""
    
    def __init__(self, commands_info: dict):
        super().__init__(timeout=300)  # 5 minute timeout
        self.commands_info = commands_info
        self.current_page = 0
        
        # Group commands by category
        self.categories = {}
        for cmd_name, cmd_info in commands_info.items():
            category = cmd_info["category"]
            if category not in self.categories:
                self.categories[category] = []
            self.categories[category].append((cmd_name, cmd_info))
        
        # Convert to list for easier pagination
        self.category_list = list(self.categories.items())
        self.total_pages = len(self.category_list)
        
        # Update button states
        self._update_buttons()
    
    def _update_buttons(self):
        """Update button states based on current page"""
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1
        
        # Update page indicator
        self.page_indicator.label = f"Page {self.current_page + 1}/{self.total_pages}"
    
    def create_embed(self) -> discord.Embed:
        """Create embed for current page"""
        if not self.category_list:
            return discord.Embed(title="📚 VN Commands Help", description="No commands available.", color=0x3498db)
        
        category_name, commands = self.category_list[self.current_page]
        
        embed = discord.Embed(
            title="📚 VN Commands Help",
            description=f"**{category_name}**\n\nUse `/help command:<name>` for detailed help on a specific command.",
            color=0x3498db
        )
        
        # Add commands in this category
        for cmd_name, cmd_info in commands:
            admin_indicator = " 🔒" if cmd_info["admin_only"] else ""
            embed.add_field(
                name=f"/{cmd_name}{admin_indicator}",
                value=f"{cmd_info['description']}\n`{cmd_info['usage']}`",
                inline=False
            )
        
        # Add general info footer
        embed.add_field(
            name="💡 Quick Tips",
            value="• Use `/help command:<name>` for detailed help\n• 🔒 = Admin only command\n• All VN commands start with `/vn`",
            inline=False
        )
        
        embed.set_footer(text=f"VNCR • Help System • Page {self.current_page + 1}/{self.total_pages}")
        return embed
    
    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=True)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Page indicator (disabled button)"""
        await interaction.response.defer()
    
    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page"""
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="📖 Command List", style=discord.ButtonStyle.success)
    async def command_list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show quick command list"""
        all_commands = []
        for category_name, commands in self.category_list:
            category_commands = []
            for cmd_name, cmd_info in commands:
                admin_indicator = " 🔒" if cmd_info["admin_only"] else ""
                category_commands.append(f"• `/{cmd_name}`{admin_indicator}")
            all_commands.append(f"**{category_name}**\n" + "\n".join(category_commands))
        
        command_list_text = "\n\n".join(all_commands)
        
        embed = discord.Embed(
            title="📋 All VN Commands",
            description=command_list_text + "\n\n💡 Use `/help command:<name>` for detailed help on any command.",
            color=0x2ecc71
        )
        embed.set_footer(text="VNCR • Quick Command Reference")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_timeout(self):
        """Handle view timeout"""
        # Disable all buttons
        for item in self.children:
            item.disabled = True


class VNMonthCog(commands.Cog):
    """Visual Novel of the Month functionality"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = Database()
    
    async def cog_load(self):
        """Initialize database when cog loads"""
        await self.db.initialize()
        logger.info("VN Month cog loaded and database initialized")
    
    MAX_BANNERS_PER_RESPONSE = 4

    @staticmethod
    def _build_vn_links_view(vndb_id: str, jiten_deck_id: Optional[int]) -> discord.ui.View:
        """Link buttons attached to each banner: VNDB + VN Club always, jiten only if known."""
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="VNDB",
            style=discord.ButtonStyle.link,
            url=f"https://vndb.org/{vndb_id}",
        ))
        if jiten_deck_id is not None:
            view.add_item(discord.ui.Button(
                label="jiten.moe",
                style=discord.ButtonStyle.link,
                url=f"https://jiten.moe/decks/media/{jiten_deck_id}/detail",
            ))
        view.add_item(discord.ui.Button(
            label="VN Club",
            style=discord.ButtonStyle.link,
            url=f"https://vnclub.org/vn/{vndb_id}",
        ))
        return view

    async def _send_monthly_banners(self, interaction: discord.Interaction,
                                    vns_data: list, target_month: int, target_year: int):
        """Render one banner per VN (capped) and send each as its own followup."""
        month_label = f"{get_month_name(target_month)} {target_year}"
        month_days = days_in_month(target_year, target_month)

        capped = vns_data[: self.MAX_BANNERS_PER_RESPONSE]
        truncated = len(vns_data) > self.MAX_BANNERS_PER_RESPONSE

        caption = f"**VN of the Month: {month_label}**"
        if truncated:
            caption += f"\n_(showing first {self.MAX_BANNERS_PER_RESPONSE} of {len(vns_data)} VNs)_"

        async with MonthlyBannerGenerator() as gen, VNDBClient() as vndb, JitenClient() as jiten:
            for index, vn_data in enumerate(capped):
                vndb_id = vn_data['vndb_id']
                fresh = await vndb.get_vn_by_id(vndb_id) or {}
                title = fresh.get('title') or vn_data.get('title') or vndb_id

                titles = fresh.get('titles') or []
                subtitle = None
                for t in titles:
                    latin = t.get('latin')
                    if latin and latin != title:
                        subtitle = latin
                        break

                cover_url = (fresh.get('image') or {}).get('url') or vn_data.get('image_url')
                cover_nsfw = is_content_nsfw(fresh) if fresh else False
                length_tier = format_vn_length(fresh.get('length') or vn_data.get('length'))

                vndb_rating = fresh.get('rating')
                vndb_votes = fresh.get('votecount')
                released = fresh.get('released') or vn_data.get('released')
                release_year = released.split('-', 1)[0] if isinstance(released, str) and released[:4].isdigit() else None

                raw_tags = fresh.get('tags') or []
                content_tags = [
                    t for t in raw_tags
                    if t.get('category') == 'cont' and (t.get('spoiler') or 0) == 0 and t.get('name')
                ]
                content_tags.sort(key=lambda t: t.get('rating') or 0, reverse=True)
                top_tags = [t['name'] for t in content_tags[:5]]

                info = await jiten.get_by_vndb_id(vndb_id)
                if info:
                    total_chars = info.character_count or None
                    difficulty = info.difficulty_raw or None
                    unique_kanji = info.unique_kanji_count or None
                    dialogue_pct = info.dialogue_percentage or None
                    jiten_deck_id = info.deck_id
                    if cover_nsfw and info.cover_url:
                        # jiten cropped covers are work-safe
                        cover_url = info.cover_url
                        cover_nsfw = False
                else:
                    total_chars = None
                    difficulty = None
                    unique_kanji = None
                    dialogue_pct = None
                    jiten_deck_id = None

                cpd = chars_per_day(total_chars, target_year, target_month) if total_chars else None

                png = await gen.generate(
                    title=title,
                    subtitle=subtitle,
                    cover_url=cover_url,
                    cover_is_nsfw=cover_nsfw,
                    month_label=month_label,
                    month_days=month_days,
                    total_chars=total_chars,
                    chars_per_day=cpd,
                    difficulty=difficulty,
                    length_tier=length_tier,
                    vndb_rating=vndb_rating,
                    vndb_votecount=vndb_votes,
                    unique_kanji=unique_kanji,
                    dialogue_pct=dialogue_pct,
                    release_year=release_year,
                    top_tags=top_tags,
                )

                file = discord.File(
                    png,
                    filename=f"vnmonth_{vndb_id}_{target_year}_{target_month:02d}.png",
                )
                view = self._build_vn_links_view(vndb_id, jiten_deck_id)
                # The first followup carries the month caption; the rest are quiet.
                content = caption if index == 0 else None
                await interaction.followup.send(content=content, file=file, view=view)

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnmonthly", description="View the current VN(s) of the month")
    # @app_commands.describe(
    #     month="Target month (1-12, defaults to current month)",
    #     year="Target year (defaults to current year)",
    #     banner="Generate a shareable PNG banner with reading stats"
    # )
    async def vnmonthly(self, interaction: discord.Interaction,
                       month: Optional[int] = None,
                       year: Optional[int] = None,
                       banner: bool = False):
        """Display the VN(s) of the month"""
        await interaction.response.defer()

        try:
            # Get all VNs of the month for the specified period
            vns_data = await self.db.get_vns_of_month_from_pool(month, year)

            # Get the target month/year for display
            if month is None or year is None:
                current_date = datetime.now()
                target_month = month or current_date.month
                target_year = year or current_date.year
            else:
                target_month = month
                target_year = year

            month_name = get_month_name(target_month)

            if not vns_data:
                await interaction.followup.send(f"No VN of the month set for {month_name} {target_year}")
                return

            if banner:
                await self._send_monthly_banners(interaction, vns_data, target_month, target_year)
                return
            
            # If there's only one VN, show the detailed single view
            if len(vns_data) == 1:
                vn_data = vns_data[0]
                
                # Create embed with pool data (fast response)
                embed = discord.Embed(
                    title=f"VN of the Month - {vn_data['title']}",
                    color=discord.Color.purple(),
                    timestamp=datetime.utcnow()
                )
                
                # Add month/year info
                embed.add_field(
                    name="Month", 
                    value=f"{month_name} {target_year}", 
                    inline=True
                )
                
                # Use cached data if available
                if vn_data.get('description'):
                    formatted_description = convert_bbcode_to_markdown(vn_data['description'])
                    embed.description = truncate_text(formatted_description, 1000)
                
                # Handle image safely - fetch fresh VNDB data to check with current threshold
                vndb_id = vn_data['vndb_id']
                async with VNDBClient() as vndb:
                    fresh_vn_data = await vndb.get_vn_by_id(vndb_id)
                
                # Check if NSFW using current threshold (not cached db flag)
                if fresh_vn_data and is_content_nsfw(fresh_vn_data):
                    embed.add_field(
                        name="Cover",
                        value="🔞 Cover hidden (marked explicit on VNDB)",
                        inline=False
                    )
                elif vn_data.get('image_url'):
                    embed.set_image(url=vn_data['image_url'])
                
                if vn_data.get('rating'):
                    embed.add_field(
                        name="Rating", 
                        value=format_rating(vn_data['rating']), 
                        inline=True
                    )
                
                if vn_data.get('length'):
                    embed.add_field(
                        name="Length", 
                        value=format_vn_length(vn_data['length']), 
                        inline=True
                    )
                
                # Calculate dual scoring for VN of the Month
                base_score = calculate_vn_score(
                    length=vn_data.get('length'),
                    is_vn_of_month=True,
                    completed_in_target_month=False
                )
                
                bonus_score = calculate_vn_score(
                    length=vn_data.get('length'),
                    is_vn_of_month=True,
                    completed_in_target_month=True
                )
                
                # Build bonus descriptions
                base_bonuses = []
                bonus_bonuses = []
                
                # Add length bonus if applicable
                if vn_data.get('length'):
                    length_formatted = format_vn_length(vn_data['length'])
                    if length_formatted != "Unknown":
                        length_multipliers = {
                            "Very Short": "1x",
                            "Short": "1.2x", 
                            "Medium": "1.6x",
                            "Long": "2x",
                            "Very Long": "2.5x"
                        }
                        multiplier = length_multipliers.get(length_formatted, "1x")
                        if multiplier != "1x":
                            base_bonuses.append(f"{length_formatted}: {multiplier}")
                            bonus_bonuses.append(f"{length_formatted}: {multiplier}")
                
                # Add VN of Month bonus
                base_bonuses.append("VN of Month: 2x")
                bonus_bonuses.append("VN of Month: 2x")
                bonus_bonuses.append("Target month: 1.5x")
                
                # Format the dual score text
                score_text = f"**Out-of-month:** {base_score} pts"
                if base_bonuses:
                    score_text += f" ({', '.join(base_bonuses)})"
                
                score_text += f"\n**In-month:** {bonus_score} pts"
                if bonus_bonuses:
                    score_text += f" ({', '.join(bonus_bonuses)})"
                
                embed.add_field(
                    name="Score",
                    value=score_text,
                    inline=True
                )
                
                # Add suggester if available
                if vn_data.get('added_by_name'):
                    embed.add_field(
                        name="Suggested by", 
                        value=vn_data['added_by_name'], 
                        inline=True
                    )
                
                embed.add_field(
                    name="VNDB Link", 
                    value=f"[View on VNDB](https://vndb.org/{vn_data['vndb_id']})", 
                    inline=False
                )
                
                embed.set_footer(text="VNCR • VN of the Month")
                await interaction.followup.send(embed=embed)
            
            else:
                # Multiple VNs - show a list view
                embed = discord.Embed(
                    title=f"VNs of the Month - {month_name} {target_year}",
                    description=f"**{len(vns_data)} VNs featured this month**",
                    color=discord.Color.purple(),
                    timestamp=datetime.utcnow()
                )
                
                # Add each VN as a field
                for i, vn_data in enumerate(vns_data, 1):
                    # Calculate scores
                    base_score = calculate_vn_score(
                        length=vn_data.get('length'),
                        is_vn_of_month=True,
                        completed_in_target_month=False
                    )
                    
                    bonus_score = calculate_vn_score(
                        length=vn_data.get('length'),
                        is_vn_of_month=True,
                        completed_in_target_month=True
                    )
                    
                    # Build field text
                    field_text = f"**Pool ID:** {vn_data['id']}\n"
                    field_text += f"**VNDB:** [{vn_data['vndb_id']}](https://vndb.org/{vn_data['vndb_id']})\n"
                    
                    if vn_data.get('length'):
                        field_text += f"**Length:** {format_vn_length(vn_data['length'])}\n"
                    
                    if vn_data.get('rating'):
                        field_text += f"**Rating:** {format_rating(vn_data['rating'])}\n"
                    
                    field_text += f"**Score:** {base_score} pts / {bonus_score} pts (in-month)\n"
                    
                    if vn_data.get('added_by_name'):
                        field_text += f"**Suggested by:** {vn_data['added_by_name']}"
                    
                    embed.add_field(
                        name=f"{i}. {vn_data['title']}",
                        value=field_text,
                        inline=False
                    )
                
                embed.add_field(
                    name="💡 Tip",
                    value="Use `/vnentry pool_id:X` to view detailed information about any specific VN",
                    inline=False
                )
                
                embed.set_footer(text="VNCR • VNs of the Month")
                await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in vnmonthly command: {e}")
            await interaction.followup.send("An error occurred while fetching VN data.")
    
    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnentry", description="View detailed information about a specific pool entry")
    async def vnentry(self, interaction: discord.Interaction, pool_id: int):
        """Show detailed information about a specific VN pool entry"""
        await interaction.response.defer()
        
        try:
            # Get the specific pool entry
            pool_entry = await self.db.get_pool_entry_by_id(pool_id)
            
            if not pool_entry:
                await interaction.followup.send(f"❌ No VN found in pool with ID {pool_id}. Use `/vnpool` to see available VNs and their IDs.")
                return
            
            # Create detailed embed
            embed = discord.Embed(
                title=f"Pool Entry #{pool_id} - {pool_entry['title']}",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            # Basic information
            embed.add_field(
                name="VNDB ID",
                value=f"[{pool_entry['vndb_id']}](https://vndb.org/{pool_entry['vndb_id']})",
                inline=True
            )
            
            # Status information
            status = pool_entry.get('status')
            if status == 'vn_of_month':
                status_text = "🏆 **VN of the Month**"
            else:
                status_text = "📋 In Pool"
            
            embed.add_field(
                name="Status",
                value=status_text,
                inline=True
            )
            
            # Target month information
            if pool_entry.get('target_month') and pool_entry.get('target_year'):
                month_name = get_month_name(pool_entry['target_month'])
                embed.add_field(
                    name="Target Month",
                    value=f"{month_name} {pool_entry['target_year']}",
                    inline=True
                )
            else:
                embed.add_field(
                    name="Target Month",
                    value="Not specified",
                    inline=True
                )
            
            # Suggester information
            if pool_entry.get('added_by_name'):
                embed.add_field(
                    name="Suggested by",
                    value=pool_entry['added_by_name'],
                    inline=True
                )
            
            # Creation date
            if pool_entry.get('created_at'):
                embed.add_field(
                    name="Added On",
                    value=pool_entry['created_at'][:10],  # Just the date part
                    inline=True
                )
            
            # VN metadata if available
            if pool_entry.get('rating'):
                embed.add_field(
                    name="Rating",
                    value=format_rating(pool_entry['rating']),
                    inline=True
                )
            
            if pool_entry.get('length'):
                embed.add_field(
                    name="Length",
                    value=format_vn_length(pool_entry['length']),
                    inline=True
                )
            
            # Calculate and display both out-of-month and in-month scores
            is_monthly = pool_entry.get('status') == 'vn_of_month'
            
            # Calculate base score (out-of-month)
            base_score = calculate_vn_score(
                length=pool_entry.get('length'),
                is_vn_of_month=is_monthly,
                completed_in_target_month=False  # No target month bonus
            )
            
            # Calculate in-month bonus score
            bonus_score = calculate_vn_score(
                length=pool_entry.get('length'),
                is_vn_of_month=is_monthly,
                completed_in_target_month=True  # With target month bonus
            )
            
            # Build list of base bonuses
            base_bonuses = []
            
            # Add length bonus if applicable
            if pool_entry.get('length'):
                length_formatted = format_vn_length(pool_entry['length'])
                if length_formatted != "Unknown":
                    # Get the multiplier for this length
                    length_multipliers = {
                        "Very Short": "1x",
                        "Short": "1.2x", 
                        "Medium": "1.6x",
                        "Long": "2x",
                        "Very Long": "2.5x"
                    }
                    multiplier = length_multipliers.get(length_formatted, "1x")
                    if multiplier != "1x":  # Only show non-1x multipliers
                        base_bonuses.append(f"{length_formatted}: {multiplier}")
            
            # Add VN of Month bonus if applicable
            if is_monthly:
                base_bonuses.append("VN of Month: 2x")
            
            # Format the dual score text
            score_text = f"**Out-of-month:** {base_score} pts"
            if base_bonuses:
                score_text += f" ({', '.join(base_bonuses)})"
            
            score_text += f"\n**In-month:** {bonus_score} pts"
            if base_bonuses:
                score_text += f" ({', '.join(base_bonuses)}, Target month: 1.5x)"
            else:
                score_text += " (Target month: 1.5x)"
            
            embed.add_field(
                name="Score",
                value=score_text,
                inline=False  # Make this wider since it has two lines
            )
            
            # Description
            if pool_entry.get('description'):
                formatted_description = convert_bbcode_to_markdown(pool_entry['description'])
                embed.description = truncate_text(formatted_description, 1000)
            
            # Cover image - handle NSFW content safely
            # Fetch fresh VNDB data to check with current threshold
            vndb_id = pool_entry['vndb_id']
            async with VNDBClient() as vndb:
                fresh_vn_data = await vndb.get_vn_by_id(vndb_id)
            
            # Check if NSFW using current threshold (not cached db flag)
            if fresh_vn_data and is_content_nsfw(fresh_vn_data):
                embed.add_field(
                    name="Cover",
                    value="🔞 Cover hidden (marked explicit on VNDB)",
                    inline=False
                )
            elif pool_entry.get('image_url'):
                embed.set_image(url=pool_entry['image_url'])
            elif pool_entry.get('image', {}).get('url'):
                embed.set_image(url=pool_entry['image']['url'])
            
            embed.set_footer(text="VNCR • Pool Entry Details")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in vnentry command: {e}")
            await interaction.followup.send("An error occurred while retrieving pool entry details.")
    
    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnpool", description="View VNs in the pool")
    # @app_commands.describe(
    #     month="Target month (1-12, defaults to current month)",
    #     year="Target year (defaults to current year)",
    #     filter="Filter pool entries by type (optional)"
    # )
    # @app_commands.choices(filter=[
    #     app_commands.Choice(name="Only Monthlies", value="monthly"),
    #     app_commands.Choice(name="Suggested VNs (Non-Monthly)", value="pool")
    # ])
    async def vnpool(self, interaction: discord.Interaction,
                    month: Optional[int] = None, year: Optional[int] = None,
                    filter: Optional[str] = None):
        """Show VNs in the pool with pagination by month"""
        await interaction.response.defer()
        
        try:
            # Use current month/year if not specified
            current_date = datetime.now()
            if month is None:
                month = current_date.month
            if year is None:
                year = current_date.year
            
            # Get VNs for the specified month/year
            pool_vns = await self.db.get_vn_pool(month, year, 25)
            
            # Apply status filter if specified
            if filter == "monthly":
                pool_vns = [vn for vn in pool_vns if vn.get('status') == 'vn_of_month']
            elif filter == "pool":
                pool_vns = [vn for vn in pool_vns if vn.get('status') != 'vn_of_month']
            # If filter is None, show all VNs (no filtering)
            
            # Create embed
            embed = discord.Embed(
                title="VN Pool",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            
            month_name = get_month_name(month)
            
            # Build description based on filter
            if filter == "monthly":
                embed.description = f"**VN of the Month** entries for **{month_name} {year}**"
            elif filter == "pool":
                embed.description = f"Suggested VNs (non-monthly) for **{month_name} {year}**"
            else:
                embed.description = f"VNs targeted for **{month_name} {year}**"
            
            if not pool_vns:
                if filter == "monthly":
                    no_vns_message = f"No VN of the Month entries found for {month_name} {year}"
                elif filter == "pool":
                    no_vns_message = f"No suggested VNs (non-monthly) found for {month_name} {year}"
                else:
                    no_vns_message = f"No VNs are targeted for {month_name} {year}"
                
                embed.add_field(
                    name="No VNs Found",
                    value=no_vns_message,
                    inline=False
                )
            else:
                # Show VNs for this month
                for i, vn in enumerate(pool_vns[:20]):  # Show up to 20
                    vn_text = f"**ID:** {vn['id']} • [VNDB](https://vndb.org/{vn['vndb_id']})"
                    
                    # Check if this VN is currently VN of the month using status field
                    if vn.get('status') == 'vn_of_month':
                        vn_text += f"\n👑 **VN of the Month**"
                    
                    if vn.get('rating'):
                        vn_text += f" • {format_rating(vn['rating'])}"
                    if vn.get('added_by_name'):
                        vn_text += f"\n*Suggested by {vn['added_by_name']}*"
                    
                    # Calculate score for this VN
                    is_monthly = vn.get('status') == 'vn_of_month'
                    score = calculate_vn_score(
                        length=vn.get('length'),
                        is_vn_of_month=is_monthly,
                        completed_in_target_month=False
                    )
                    
                    # Create title with number and score
                    title_text = f"{i+1}. {vn['title'][:40]} ({score} points)"
                    
                    embed.add_field(
                        name=title_text,
                        value=vn_text,
                        inline=True
                    )
                
                if len(pool_vns) > 20:
                    embed.set_footer(text=f"VNCR • Showing 20 of {len(pool_vns)} entries for {month_name} {year}")
                else:
                    embed.set_footer(text=f"VNCR • {len(pool_vns)} entries for {month_name} {year}")
            
            # Create navigation view with filter preserved
            view = PoolNavigationView(month, year, filter)
            await interaction.followup.send(embed=embed, view=view)
            
        except Exception as e:
            logger.error(f"Error in vnpool command: {e}")
            await interaction.followup.send("An error occurred while retrieving VN pool.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnaddpool", description="[ADMIN] Add a VN to the pool")
    # @app_commands.default_permissions(administrator=True)
    # @app_commands.guild_only()
    # @app_commands.describe(
    #     vndb_url="VNDB URL or ID (e.g., https://vndb.org/v123 or v123)",
    #     month="Target month (1-12, defaults to current month)",
    #     year="Target year (defaults to current year)",
    #     suggested_by="User who suggested this VN (optional, defaults to you)"
    # )
    async def vnaddpool(self, interaction: discord.Interaction, vndb_url: str,
                       month: Optional[int] = None, year: Optional[int] = None,
                       suggested_by: Optional[discord.Member] = None):
        """Add a VN to the pool"""
        await interaction.response.defer()
        
        try:
            # Default to current month/year if not specified
            current_date = datetime.now()
            if month is None:
                month = current_date.month
            if year is None:
                year = current_date.year
            
            # Validate month
            if not (1 <= month <= 12):
                await interaction.followup.send("❌ Month must be between 1 and 12")
                return
            
            # Extract VNDB ID
            vndb_id = extract_vndb_id(vndb_url)
            if not vndb_id:
                await interaction.followup.send("❌ Invalid VNDB URL or ID format")
                return
            
            # Log the lookup parameters
            logger.info(f"vnaddpool: Checking vndb_id={vndb_id}, month={month}, year={year}")
            
            # Check if already in pool for this specific month/year
            existing = await self.db.get_pool_entry_by_vndb_id_and_month(vndb_id, month, year)
            logger.info(f"vnaddpool: Query returned existing={existing}")
            
            if existing:
                month_name = get_month_name(month)
                pool_id = existing.get('id', 'Unknown')
                await interaction.followup.send(
                    f"❌ VN {vndb_id} is already in the pool for {month_name} {year} (Pool ID: {pool_id})"
                )
                return
            
            # Get VN data from VNDB
            async with VNDBClient() as vndb:
                vn_data = await vndb.get_vn_by_id(vndb_id)
            
            if not vn_data:
                await interaction.followup.send(f"❌ Could not find VN with ID {vndb_id} on VNDB")
                return
            
            # Determine who suggested this VN
            suggester = suggested_by if suggested_by else interaction.user
            
            # Add to pool
            pool_id = await self.db.add_vn_to_pool(
                vndb_id=vndb_id,
                title=vn_data.get('title', 'Unknown Title'),
                description=vn_data.get('description'),
                image_url=vn_data.get('image', {}).get('url'),
                rating=vn_data.get('rating'),
                length=vn_data.get('length'),
                added_by_id=suggester.id,
                added_by_name=suggester.display_name,
                target_month=month,
                target_year=year,
                vn_data=vn_data
            )
            
            # Create success embed
            embed = discord.Embed(
                title="✅ VN Added to Pool",
                description=f"**{vn_data.get('title', 'Unknown Title')}** has been added to the pool",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(name="Pool ID", value=str(pool_id), inline=True)
            embed.add_field(name="VNDB ID", value=vndb_id, inline=True)
            
            month_name = get_month_name(month)
            embed.add_field(name="Target", value=f"{month_name} {year}", inline=True)
            
            # Calculate dual scoring for pool entry
            base_score = calculate_vn_score(
                length=vn_data.get('length'),
                is_vn_of_month=False,  # Not VN of Month when added
                completed_in_target_month=False  # Out-of-month score
            )
            
            bonus_score = calculate_vn_score(
                length=vn_data.get('length'),
                is_vn_of_month=False,  # Not VN of Month when added
                completed_in_target_month=True  # In target month score
            )
            
            # Build bonus descriptions
            base_bonuses = []
            bonus_bonuses = []
            
            # Add length bonus if applicable
            if vn_data.get('length'):
                length_formatted = format_vn_length(vn_data['length'])
                if length_formatted != "Unknown":
                    length_multipliers = {
                        "Very Short": "1x",
                        "Short": "1.2x", 
                        "Medium": "1.6x",
                        "Long": "2x",
                        "Very Long": "2.5x"
                    }
                    multiplier = length_multipliers.get(length_formatted, "1x")
                    if multiplier != "1x":
                        base_bonuses.append(f"{length_formatted}: {multiplier}")
                        bonus_bonuses.append(f"{length_formatted}: {multiplier}")
            
            # Add target month bonus for in-month completion
            bonus_bonuses.append("Target month: 1.5x")
            
            # Format the dual score text
            score_text = f"**Out-of-month:** {base_score} pts"
            if base_bonuses:
                score_text += f" ({', '.join(base_bonuses)})"
            elif base_score == 100:
                score_text += " (base)"
            
            score_text += f"\n**In-month:** {bonus_score} pts"
            if bonus_bonuses:
                score_text += f" ({', '.join(bonus_bonuses)})"
            
            embed.add_field(
                name="Worth",
                value=score_text,
                inline=True
            )
            
            embed.add_field(name="Suggested by", value=suggester.display_name, inline=True)
            
            # Handle cover display - check NSFW status
            if is_content_nsfw(vn_data):
                embed.add_field(
                    name="Cover",
                    value="🔞 Cover hidden (marked explicit on VNDB)",
                    inline=False
                )
            elif vn_data.get('image', {}).get('url'):
                embed.set_thumbnail(url=vn_data['image']['url'])
            
            embed.set_footer(text="VNCR • VN Pool Management")
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in vnaddpool command: {e}")
            await interaction.followup.send("An error occurred while adding VN to pool.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnsetmonthly", description="[ADMIN] Set a VN as a monthly VN (multiple allowed per month)")
    # @app_commands.default_permissions(administrator=True)
    # @app_commands.guild_only()
    # @app_commands.describe(
    #     pool_id="Pool ID of the VN to set as monthly",
    #     month="Month (1-12, optional - defaults to current)",
    #     year="Year (optional - defaults to current)"
    # )
    async def vnsetmonthly(self, interaction: discord.Interaction, pool_id: int,
                          month: Optional[int] = None, year: Optional[int] = None):
        """Set a VN from the pool as VN of the month"""
        await interaction.response.defer()
        
        try:
            # Use current month/year if not specified
            current_date = datetime.now()
            if month is None:
                month = current_date.month
            if year is None:
                year = current_date.year
            
            # Validate month
            if not (1 <= month <= 12):
                await interaction.followup.send("❌ Month must be between 1 and 12")
                return
            
            # Get the pool entry
            pool_entry = await self.db.get_pool_entry_by_id(pool_id)
            if not pool_entry:
                await interaction.followup.send(f"❌ No VN found in pool with ID {pool_id}")
                return
            
            # Check if this specific VN is already VN of the month for this period
            if pool_entry.get('status') == 'vn_of_month' and pool_entry.get('target_month') == month and pool_entry.get('target_year') == year:
                await interaction.followup.send(
                    f"❌ **{pool_entry['title']}** is already set as VN of the month for {get_month_name(month)} {year}."
                )
                return
            
            # Set as VN of the month
            success = await self.db.set_vn_of_month_from_pool(pool_id, month, year)
            
            if success:
                # Check how many VNs are now set for this month
                all_monthlies = await self.db.get_vns_of_month_from_pool(month, year)
                
                embed = discord.Embed(
                    title="🏆 VN of the Month Set",
                    description=f"**{pool_entry['title']}** is now a VN of the month!",
                    color=discord.Color.gold(),
                    timestamp=datetime.utcnow()
                )
                
                month_name = get_month_name(month)
                embed.add_field(name="Month", value=f"{month_name} {year}", inline=True)
                embed.add_field(name="Pool ID", value=str(pool_id), inline=True)
                embed.add_field(name="Total Monthly VNs", value=str(len(all_monthlies)), inline=True)
                embed.add_field(name="VNDB", value=f"[View](https://vndb.org/{pool_entry['vndb_id']})", inline=False)
                
                # Calculate dual scoring for VN of the Month
                base_score = calculate_vn_score(
                    length=pool_entry.get('length'),
                    is_vn_of_month=True,  # This is now VN of the Month
                    completed_in_target_month=False  # Base VN of Month score
                )
                
                bonus_score = calculate_vn_score(
                    length=pool_entry.get('length'),
                    is_vn_of_month=True,  # This is now VN of the Month
                    completed_in_target_month=True  # With target month bonus
                )
                
                # Build bonus descriptions
                base_bonuses = []
                bonus_bonuses = []
                
                # Add length bonus if applicable
                if pool_entry.get('length'):
                    length_formatted = format_vn_length(pool_entry['length'])
                    if length_formatted != "Unknown":
                        length_multipliers = {
                            "Very Short": "1x",
                            "Short": "1.2x", 
                            "Medium": "1.6x",
                            "Long": "2x",
                            "Very Long": "2.5x"
                        }
                        multiplier = length_multipliers.get(length_formatted, "1x")
                        if multiplier != "1x":
                            base_bonuses.append(f"{length_formatted}: {multiplier}")
                            bonus_bonuses.append(f"{length_formatted}: {multiplier}")
                
                # Add VN of Month bonus
                base_bonuses.append("VN of Month: 2x")
                bonus_bonuses.append("VN of Month: 2x")
                bonus_bonuses.append("Target month: 1.5x")
                
                # Format the dual score text
                score_text = f"**Out-of-month:** {base_score} pts"
                if base_bonuses:
                    score_text += f" ({', '.join(base_bonuses)})"
                
                score_text += f"\n**In-month:** {bonus_score} pts"
                if bonus_bonuses:
                    score_text += f" ({', '.join(bonus_bonuses)})"
                
                embed.add_field(
                    name="Worth",
                    value=score_text,
                    inline=True
                )
                
                # Handle cover display - check NSFW status
                # Fetch fresh VNDB data to check with current threshold
                vndb_id = pool_entry['vndb_id']
                async with VNDBClient() as vndb:
                    fresh_vn_data = await vndb.get_vn_by_id(vndb_id)
                
                # Check if NSFW using current threshold (not cached db flag)
                if fresh_vn_data and is_content_nsfw(fresh_vn_data):
                    embed.add_field(
                        name="Cover",
                        value="🔞 Cover hidden (marked explicit on VNDB)",
                        inline=False
                    )
                elif pool_entry.get('image_url'):
                    embed.set_thumbnail(url=pool_entry['image_url'])
                
                embed.set_footer(text="VNCR • VN of the Month")
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ Failed to set VN of the month")
                
        except Exception as e:
            logger.error(f"Error in vnsetmonthly command: {e}")
            await interaction.followup.send("An error occurred while setting VN of the month.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnremovemonthly", description="[ADMIN] Remove monthly status from a VN")
    # @app_commands.default_permissions(administrator=True)
    # @app_commands.guild_only()
    # @app_commands.describe(pool_id="Pool ID of the VN to remove monthly status from")
    async def vnremovemonthly(self, interaction: discord.Interaction, pool_id: int):
        """Remove VN of the month status from a pool entry"""
        await interaction.response.defer()
        
        try:
            # Get the pool entry to check if it exists and has monthly status
            pool_entry = await self.db.get_pool_entry_by_id(pool_id)
            if not pool_entry:
                await interaction.followup.send(f"❌ No VN found in pool with ID {pool_id}")
                return
            
            # Check if it actually has monthly status
            if pool_entry.get('status') != 'vn_of_month':
                await interaction.followup.send(f"❌ **{pool_entry['title']}** (ID: {pool_id}) is not currently set as VN of the month")
                return
            
            # Remove monthly status
            success = await self.db.remove_vn_of_month_status_by_id(pool_id)
            
            if success:
                embed = discord.Embed(
                    title="📋 Monthly Status Removed",
                    description=f"**{pool_entry['title']}** is no longer VN of the month",
                    color=discord.Color.orange(),
                    timestamp=datetime.utcnow()
                )
                
                embed.add_field(name="Pool ID", value=str(pool_id), inline=True)
                embed.add_field(name="VNDB", value=f"[View](https://vndb.org/{pool_entry['vndb_id']})", inline=True)
                embed.add_field(name="Status", value="Returned to pool", inline=True)
                
                # Calculate dual scoring after removing VN of Month status
                base_score = calculate_vn_score(
                    length=pool_entry.get('length'),
                    is_vn_of_month=False,  # No longer VN of Month
                    completed_in_target_month=False  # Out-of-month score
                )
                
                bonus_score = calculate_vn_score(
                    length=pool_entry.get('length'),
                    is_vn_of_month=False,  # No longer VN of Month
                    completed_in_target_month=True  # In target month score
                )
                
                # Build bonus descriptions
                base_bonuses = []
                bonus_bonuses = []
                
                # Add length bonus if applicable
                if pool_entry.get('length'):
                    length_formatted = format_vn_length(pool_entry['length'])
                    if length_formatted != "Unknown":
                        length_multipliers = {
                            "Very Short": "1x",
                            "Short": "1.2x", 
                            "Medium": "1.6x",
                            "Long": "2x",
                            "Very Long": "2.5x"
                        }
                        multiplier = length_multipliers.get(length_formatted, "1x")
                        if multiplier != "1x":
                            base_bonuses.append(f"{length_formatted}: {multiplier}")
                            bonus_bonuses.append(f"{length_formatted}: {multiplier}")
                
                # Add target month bonus for in-month completion
                bonus_bonuses.append("Target month: 1.5x")
                
                # Format the dual score text
                score_text = f"**Out-of-month:** {base_score} pts"
                if base_bonuses:
                    score_text += f" ({', '.join(base_bonuses)})"
                elif base_score == 100:
                    score_text += " (base)"
                
                score_text += f"\n**In-month:** {bonus_score} pts"
                if bonus_bonuses:
                    score_text += f" ({', '.join(bonus_bonuses)})"
                
                embed.add_field(
                    name="Now Worth",
                    value=score_text,
                    inline=True
                )
                
                embed.set_footer(text="VNCR • VN Pool Management")
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ Failed to remove monthly status")
                
        except Exception as e:
            logger.error(f"Error in vnremovemonthly command: {e}")
            await interaction.followup.send("An error occurred while removing monthly status.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vneditpool", description="[ADMIN] Edit a VN pool entry")
    # @app_commands.default_permissions(administrator=True)
    # @app_commands.guild_only()
    # @app_commands.describe(
    #     pool_id="Pool ID of the VN to edit",
    #     month="New target month (1-12, optional)",
    #     year="New target year (optional)",
    #     suggested_by="New suggester (optional)"
    # )
    async def vneditpool(self, interaction: discord.Interaction, pool_id: int,
                        month: Optional[int] = None, year: Optional[int] = None,
                        suggested_by: Optional[discord.Member] = None):
        """Edit VN pool entry details"""
        await interaction.response.defer()
        
        try:
            # Validate month if provided
            if month is not None and not (1 <= month <= 12):
                await interaction.followup.send("❌ Month must be between 1 and 12")
                return
            
            # Check that at least one field is being updated
            if month is None and year is None and suggested_by is None:
                await interaction.followup.send("❌ You must specify at least one field to update (month, year, or suggested_by)")
                return
            
            # Get existing pool entry
            pool_entry = await self.db.get_pool_entry_by_id(pool_id)
            if not pool_entry:
                await interaction.followup.send(f"❌ No VN found in pool with ID {pool_id}")
                return
            
            # Update the entry
            success = await self.db.update_pool_entry(
                pool_id=pool_id,
                target_month=month,
                target_year=year,
                added_by_id=suggested_by.id if suggested_by else None,
                added_by_name=suggested_by.display_name if suggested_by else None
            )
            
            if success:
                embed = discord.Embed(
                    title="✏️ Pool Entry Updated",
                    description=f"**{pool_entry['title']}** has been updated",
                    color=discord.Color.blue(),
                    timestamp=datetime.utcnow()
                )
                
                embed.add_field(name="Pool ID", value=str(pool_id), inline=True)
                embed.add_field(name="VNDB", value=f"[View](https://vndb.org/{pool_entry['vndb_id']})", inline=True)
                
                # Show what was updated
                updates = []
                if month is not None and year is not None:
                    month_name = get_month_name(month)
                    updates.append(f"Target: {month_name} {year}")
                elif month is not None:
                    month_name = get_month_name(month)
                    updates.append(f"Target Month: {month_name}")
                elif year is not None:
                    updates.append(f"Target Year: {year}")
                
                if suggested_by is not None:
                    updates.append(f"Suggested by: {suggested_by.display_name}")
                
                if updates:
                    embed.add_field(name="Updated", value="\n".join(updates), inline=False)
                
                embed.set_footer(text="VNCR • VN Pool Management")
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ Failed to update pool entry")
                
        except Exception as e:
            logger.error(f"Error in vneditpool command: {e}")
            await interaction.followup.send("An error occurred while editing pool entry.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnremovepool", description="[ADMIN] Remove a VN from the pool entirely")
    # @app_commands.default_permissions(administrator=True)
    # @app_commands.guild_only()
    # @app_commands.describe(pool_id="Pool ID of the VN to remove")
    async def vnremovepool(self, interaction: discord.Interaction, pool_id: int):
        """Remove VN from pool entirely"""
        await interaction.response.defer()
        
        try:
            # Get the pool entry before deletion
            pool_entry = await self.db.get_pool_entry_by_id(pool_id)
            if not pool_entry:
                await interaction.followup.send(f"❌ No VN found in pool with ID {pool_id}")
                return
            
            # Check if it's currently VN of the month
            if pool_entry.get('status') == 'vn_of_month':
                month_name = get_month_name(pool_entry['target_month']) if pool_entry.get('target_month') else "Unknown"
                year = pool_entry.get('target_year', 'Unknown')
                await interaction.followup.send(
                    f"❌ **{pool_entry['title']}** is currently VN of the month for {month_name} {year}. "
                    f"Use `/vnremovemonthly {pool_id}` first to remove monthly status, then try removing from pool."
                )
                return
            
            # Remove from pool
            success = await self.db.remove_from_pool(pool_id)
            
            if success:
                embed = discord.Embed(
                    title="🗑️ VN Removed from Pool",
                    description=f"**{pool_entry['title']}** has been permanently removed from the pool",
                    color=discord.Color.red(),
                    timestamp=datetime.utcnow()
                )
                
                embed.add_field(name="Pool ID", value=str(pool_id), inline=True)
                embed.add_field(name="VNDB", value=f"[View](https://vndb.org/{pool_entry['vndb_id']})", inline=True)
                embed.add_field(name="Action", value="Permanently deleted", inline=True)
                
                embed.set_footer(text="VNCR • VN Pool Management")
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ Failed to remove VN from pool")
                
        except Exception as e:
            logger.error(f"Error in vnremovepool command: {e}")
            await interaction.followup.send("An error occurred while removing VN from pool.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vncomplete", description="Mark a VN as completed")
    # @app_commands.describe(
    #     pool_id="Pool ID of the VN you completed",
    #     rating="Your rating (1-10, optional)",
    #     review="Your review (optional, max 500 characters)",
    #     completed_date="Date you completed it (YYYY-MM-DD format, optional)"
    # )
    async def vncomplete(self, interaction: discord.Interaction, pool_id: int,
                        rating: Optional[int] = None, review: Optional[str] = None,
                        completed_date: Optional[str] = None):
        """Mark a VN as completed"""
        await interaction.response.defer()
        
        try:
            # Rate limiting: prevent rapid completion submissions (max 1 every 10 seconds)
            user_id = interaction.user.id
            now = datetime.now()
            last_completion = completion_rate_limit.get(user_id)
            
            if last_completion and (now - last_completion).total_seconds() < 10:
                remaining = 10 - int((now - last_completion).total_seconds())
                await interaction.followup.send(f"⏰ Please wait {remaining} seconds before marking another VN as completed")
                return
            
            # Validate pool_id (basic sanity check)
            if pool_id <= 0:
                await interaction.followup.send("❌ Pool ID must be a positive number")
                return
            
            # Validate rating
            if rating is not None and not (1 <= rating <= 10):
                await interaction.followup.send("❌ Rating must be between 1 and 10")
                return
            
            # Validate review length and content
            if review is not None:
                if len(review) > 500:
                    await interaction.followup.send("❌ Review must be 500 characters or less")
                    return
                
                # Basic content filtering - prevent obvious abuse
                review_lower = review.lower()
                if any(word in review_lower for word in ['@everyone', '@here', 'discord.gg/']):
                    await interaction.followup.send("❌ Review contains prohibited content")
                    return
                
                # Strip excessive whitespace
                review = review.strip()
                if not review:  # If review becomes empty after stripping
                    review = None
            # Validate rating if provided
            if rating is not None and not (1 <= rating <= 10):
                await interaction.followup.send("❌ Rating must be between 1 and 10")
                return
            
            # Validate and parse completion date if provided
            date_valid, formatted_date, date_error = parse_completion_date(completed_date)
            if not date_valid:
                await interaction.followup.send(f"❌ {date_error}")
                return
            
            # Check if pool entry exists
            pool_entry = await self.db.get_pool_entry_by_id(pool_id)
            if not pool_entry:
                await interaction.followup.send(f"❌ No VN found in pool with ID {pool_id}")
                return
            
            # Check if already completed
            already_completed = await self.db.is_vn_completed_by_user(interaction.user.id, pool_id)
            if already_completed:
                await interaction.followup.send(f"❌ You have already marked **{pool_entry['title']}** as completed. Use `/vncompletions` to see your completed VNs.")
                return
            
            # Mark as completed
            success = await self.db.mark_vn_completed(
                user_id=interaction.user.id,
                pool_id=pool_id,
                rating=rating,
                review=review,
                completed_date=formatted_date
            )
            
            if success:
                # Set rate limit timestamp
                completion_rate_limit[user_id] = now

                embed = discord.Embed(
                    title="✅ VN Marked as Completed",
                    description=f"**{pool_entry['title']}** has been added to your completed VNs!",
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                
                embed.add_field(name="Pool ID", value=str(pool_id), inline=True)
                embed.add_field(name="VNDB", value=f"[View](https://vndb.org/{pool_entry['vndb_id']})", inline=True)
                
                if rating:
                    embed.add_field(name="Your Rating", value=f"{rating}/10", inline=True)
                
                if completed_date:
                    embed.add_field(name="Completion Date", value=completed_date, inline=True)
                
                if review:
                    embed.add_field(name="Your Review", value=truncate_text(review, 500), inline=False)
                
                # Calculate and display points earned
                is_vn_of_month = pool_entry.get('status') == 'vn_of_month'
                
                # Check if completed in target month for bonus
                completed_in_target_month = False
                target_month_info = ""
                debug_info = []
                
                # Use the provided date or current date for bonus calculation
                date_to_check = formatted_date or datetime.now().strftime('%Y-%m-%d')
                
                if date_to_check and pool_entry.get('target_month') and pool_entry.get('target_year'):
                    try:
                        completion_date = datetime.strptime(date_to_check, '%Y-%m-%d')
                        completion_month = completion_date.month
                        completion_year = completion_date.year
                        target_month = int(pool_entry['target_month'])
                        target_year = int(pool_entry['target_year'])
                        
                        completed_in_target_month = (completion_month == target_month and completion_year == target_year)
                        
                        # Store target month info for display
                        target_month_name = get_month_name(target_month)
                        target_month_info = f" (Target: {target_month_name} {target_year})"
                        
                        # Debug info
                        debug_info.append(f"Completed: {completion_month}/{completion_year}")
                        debug_info.append(f"Target: {target_month}/{target_year}")
                        debug_info.append(f"Match: {completed_in_target_month}")
                        debug_info.append(f"Used date: {date_to_check}")
                        
                    except (ValueError, TypeError) as e:
                        debug_info.append(f"Date parsing error: {e}")
                        pass
                elif not date_to_check:
                    debug_info.append("No completion date available")
                elif not pool_entry.get('target_month'):
                    debug_info.append("No target month set for this VN")
                elif not pool_entry.get('target_year'):
                    debug_info.append("No target year set for this VN")
                
                score = calculate_vn_score(
                    length=pool_entry.get('length'),
                    is_vn_of_month=is_vn_of_month,
                    completed_in_target_month=completed_in_target_month
                )
                
                # Build list of bonuses applied
                bonuses = []
                
                # Add length bonus if applicable
                if pool_entry.get('length'):
                    length_formatted = format_vn_length(pool_entry['length'])
                    if length_formatted != "Unknown":
                        length_multipliers = {
                            "Very Short": "1x",
                            "Short": "1.2x", 
                            "Medium": "1.6x",
                            "Long": "2x",
                            "Very Long": "2.5x"
                        }
                        multiplier = length_multipliers.get(length_formatted, "1x")
                        if multiplier != "1x":
                            bonuses.append(f"{length_formatted}: {multiplier}")
                
                # Add VN of Month bonus
                if is_vn_of_month:
                    bonuses.append("VN of Month: 2x")
                    
                # Add target month bonus
                if completed_in_target_month:
                    bonuses.append(f"Target month: 1.5x{target_month_info}")
                elif pool_entry.get('target_month') and pool_entry.get('target_year'):
                    # Show target month info even if bonus wasn't earned
                    target_month_name = get_month_name(pool_entry['target_month'])
                    if formatted_date:
                        # User provided a specific date that wasn't in target month
                        bonuses.append(f"(Target was {target_month_name} {pool_entry['target_year']} - no bonus)")
                    else:
                        # No date provided, using current date
                        bonuses.append(f"(Target was {target_month_name} {pool_entry['target_year']} - completed today)")
                
                # Log debug info for troubleshooting
                if debug_info:
                    logger.info(f"Target month bonus debug for {pool_entry['title']}: {'; '.join(debug_info)}")
                
                # Format the score text
                score_text = f"🏆 **{score} points earned!**"
                if bonuses:
                    score_text += f"\n*Bonuses: {', '.join(bonuses)}*"
                
                embed.add_field(name="Points", value=score_text, inline=False)
                
                # Get user's updated total score
                try:
                    # Get all completions including this new one to calculate total
                    all_completions = await self.db.get_user_completions(interaction.user.id, limit=1000)
                    total_score = 0
                    for completion in all_completions:
                        # Check if completed in target month
                        comp_in_target_month = False
                        if completion.get('completed_at') and completion.get('target_month') and completion.get('target_year'):
                            completed_date_parts = completion['completed_at'][:10].split('-')
                            if len(completed_date_parts) >= 2:
                                try:
                                    completed_month = int(completed_date_parts[1])
                                    completed_year = int(completed_date_parts[0])
                                    comp_in_target_month = (completed_month == completion['target_month'] and 
                                                          completed_year == completion['target_year'])
                                except ValueError:
                                    pass
                        
                        # Check if this was VN of the month when completed
                        is_monthly = completion.get('status') == 'vn_of_month'
                        
                        comp_score = calculate_vn_score(
                            completion.get('length'), 
                            is_monthly, 
                            comp_in_target_month
                        )
                        total_score += comp_score
                    
                    embed.add_field(name="🎯 Your Total Score", value=f"**{total_score} points**", inline=True)
                except Exception as e:
                    logger.error(f"Error calculating total score: {e}")
                    # A total-score failure does not fail the whole command
                    pass
                
                # Check if this was a VN of the month
                if is_vn_of_month:
                    embed.add_field(name="🏆 Special", value="This was a VN of the Month!", inline=False)
                
                embed.set_footer(text="VNCR • VN Completion Tracking")
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ Failed to mark VN as completed")
                
        except Exception as e:
            logger.error(f"Error in vncomplete command: {e}")
            await interaction.followup.send("An error occurred while marking VN as completed.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vncompletions", description="View your completed VNs")
    # @app_commands.describe(
    #     user="User to view completions for (defaults to yourself)"
    # )
    async def vncompletions(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        """View completed VNs for a user with pagination"""
        await interaction.response.defer()
        
        try:
            target_user = user or interaction.user
            
            # Get user completions and stats
            completions = await self.db.get_user_completions(target_user.id, limit=1000)  # Get all for pagination
            stats = await self.db.get_user_completion_stats(target_user.id)
            
            if not completions:
                # Create simple embed for users with no completions
                embed = discord.Embed(
                    title=f"📚 {target_user.display_name}'s Completed VNs",
                    color=discord.Color.blue(),
                    timestamp=datetime.utcnow()
                )
                
                # Add stats
                stats_text = f"**Total Completed:** {stats['total_completed']}\n"
                if stats['average_rating']:
                    stats_text += f"**Average Rating:** {stats['average_rating']}/10\n"
                stats_text += f"**VN of the Month Completed:** {stats['vn_of_month_completed']}"
                
                embed.add_field(name="📊 Statistics", value=stats_text, inline=False)
                embed.add_field(
                    name="No Completions",
                    value=f"{target_user.display_name} hasn't completed any VNs yet.",
                    inline=False
                )
                embed.set_footer(text="VNCR • 0 completed VNs")
                
                await interaction.followup.send(embed=embed)
            else:
                # Create pagination view
                view = CompletionsNavigationView(interaction.user.id, target_user)
                embed = await view._update_embed(interaction, 1)
                await interaction.followup.send(embed=embed, view=view)
            
        except Exception as e:
            logger.error(f"Error in vncompletions command: {e}")
            await interaction.followup.send("An error occurred while retrieving completion data.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnremovecomplete", description="Remove a VN from your completed list")
    # @app_commands.describe(pool_id="Pool ID of the VN to remove from completed")
    async def vnremovecomplete(self, interaction: discord.Interaction, pool_id: int):
        """Remove a VN from user's completed list"""
        await interaction.response.defer()
        
        try:
            # Check if the completion exists
            already_completed = await self.db.is_vn_completed_by_user(interaction.user.id, pool_id)
            if not already_completed:
                await interaction.followup.send(f"❌ You haven't marked Pool ID {pool_id} as completed.")
                return
            
            # Get pool entry for display
            pool_entry = await self.db.get_pool_entry_by_id(pool_id)
            if not pool_entry:
                await interaction.followup.send(f"❌ No VN found in pool with ID {pool_id}")
                return
            
            # Remove completion
            success = await self.db.remove_completion(interaction.user.id, pool_id)
            
            if success:
                embed = discord.Embed(
                    title="🗑️ Completion Removed",
                    description=f"**{pool_entry['title']}** has been removed from your completed VNs",
                    color=discord.Color.orange(),
                    timestamp=datetime.utcnow()
                )
                
                embed.add_field(name="Pool ID", value=str(pool_id), inline=True)
                embed.add_field(name="VNDB", value=f"[View](https://vndb.org/{pool_entry['vndb_id']})", inline=True)
                
                embed.set_footer(text="VNCR • VN Completion Tracking")
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ Failed to remove completion")
                
        except Exception as e:
            logger.error(f"Error in vnremovecomplete command: {e}")
            await interaction.followup.send("An error occurred while removing completion.")

    # Disabled: VN of the Month is handled by hikaru.
    # Uncomment the decorators below to re-enable.
    # @app_commands.command(name="vnleaderboard", description="View point leaderboard")
    # @app_commands.describe(
    #     timeframe="Timeframe for leaderboard",
    #     month="Specific month (1-12, for monthly timeframe)",
    #     year="Specific year (for monthly/quarterly timeframe)"
    # )
    # @app_commands.choices(timeframe=[
    #     app_commands.Choice(name="All-Time", value="all-time"),
    #     app_commands.Choice(name="Monthly", value="monthly"),
    #     app_commands.Choice(name="Quarterly", value="quarterly")
    # ])
    async def vnleaderboard(self, interaction: discord.Interaction,
                           timeframe: app_commands.Choice[str] = None,
                           month: Optional[int] = None, 
                           year: Optional[int] = None):
        """Display point leaderboard for different timeframes"""
        await interaction.response.defer()
        
        try:
            # Handle timeframe parameter
            if timeframe is None:
                timeframe_value = "all-time"
            else:
                timeframe_value = timeframe.value
            
            # Validate timeframe
            valid_timeframes = ["all-time", "quarterly", "monthly"]
            if timeframe_value not in valid_timeframes:
                await interaction.followup.send(f"❌ Invalid timeframe. Choose from: {', '.join(valid_timeframes)}")
                return
            
            # Set defaults for month/year based on current date
            current_date = datetime.now()
            if year is None:
                year = current_date.year
            if month is None:
                month = current_date.month
            
            # Validate month for monthly timeframe
            if timeframe_value == "monthly" and not (1 <= month <= 12):
                await interaction.followup.send("❌ Month must be between 1 and 12 for monthly leaderboard")
                return
            
            # Get leaderboard data
            leaderboard_data = await self.db.get_leaderboard(timeframe_value, month, year)
            
            if not leaderboard_data:
                period_text = self._get_period_text(timeframe_value, month, year)
                await interaction.followup.send(f"No completion data found for {period_text}")
                return
            
            # Create embed
            embed = discord.Embed(
                title="🏆 VN Point Leaderboard",
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )
            
            period_text = self._get_period_text(timeframe_value, month, year)
            embed.description = f"**{period_text}**"
            
            # Add top users
            leaderboard_text = ""
            for i, user_data in enumerate(leaderboard_data[:10], 1):  # Top 10
                user_id = user_data['user_id']
                total_points = user_data['total_points']
                completion_count = user_data['completion_count']
                
                # Try to get user from Discord
                try:
                    user = await self.bot.fetch_user(user_id)
                    username = user.display_name
                except:
                    username = f"User {user_id}"
                
                # Medal emojis for top 3
                if i == 1:
                    medal = "🥇"
                elif i == 2:
                    medal = "🥈"
                elif i == 3:
                    medal = "🥉"
                else:
                    medal = f"**{i}.**"
                
                # Calculate average points per VN
                avg_points = int(total_points / completion_count) if completion_count > 0 else 0
                
                leaderboard_text += f"{medal} **{username}**\n"
                leaderboard_text += f"    {int(total_points)} pts • {completion_count} VNs • {avg_points} avg\n"
            
            embed.add_field(
                name="Rankings",
                value=leaderboard_text,
                inline=False
            )
            
            # Add summary stats
            total_users = len(leaderboard_data)
            total_completions = sum(user['completion_count'] for user in leaderboard_data)
            total_points_all = sum(user['total_points'] for user in leaderboard_data)
            
            summary_text = f"📈 **{total_users}** active users\n"
            summary_text += f"📚 **{total_completions}** total completions\n" 
            summary_text += f"🏆 **{int(total_points_all)}** total points awarded"
            
            embed.add_field(
                name="📊 Statistics",
                value=summary_text,
                inline=True
            )
            
            # Add quick guide
            if timeframe_value == "all-time":
                guide_text = "🕐 Use `/vnleaderboard timeframe:Monthly` for current month\n"
                guide_text += "📅 Use `/vnleaderboard timeframe:Quarterly` for current quarter"
            else:
                guide_text = "🕐 Use `/vnleaderboard` for all-time rankings\n"
                guide_text += f"📊 Showing {period_text.lower()}"
            
            embed.add_field(
                name="💡 Quick Tips",
                value=guide_text,
                inline=True
            )
            
            embed.set_footer(text="VNCR • Point Leaderboard")
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in vnleaderboard command: {e}")
            await interaction.followup.send("An error occurred while retrieving leaderboard data.")
    
    def _get_period_text(self, timeframe: str, month: int, year: int) -> str:
        """Get descriptive text for the time period"""
        if timeframe == "all-time":
            return "All-Time Rankings"
        elif timeframe == "monthly":
            month_name = get_month_name(month)
            return f"{month_name} {year} Rankings"
        elif timeframe == "quarterly":
            quarter = ((month - 1) // 3) + 1
            return f"Q{quarter} {year} Rankings"
        return timeframe

    

async def setup(bot):
    await bot.add_cog(VNMonthCog(bot))
