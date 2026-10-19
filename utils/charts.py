"""
Chart generation utilities for immersion tracking
Uses QuickChart.io API to generate activity charts
"""

import json
import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from io import BytesIO

logger = logging.getLogger(__name__)

# Charts are tiny PNGs; cap the response body so a misbehaving endpoint
# cannot force an arbitrarily large buffer.
MAX_CHART_BYTES = 8 * 1024 * 1024

class ChartGenerator:
    """Generates charts for immersion tracking data using QuickChart.io"""

    QUICKCHART_URL = "https://quickchart.io/chart"

    # Discord-style colors
    PRIMARY_COLOR = "#5865F2"
    SECONDARY_COLOR = "#57F287"
    BACKGROUND_COLOR = "#232428"
    GRID_COLOR = "#2B2D31"
    TEXT_COLOR = "#9e9e9e"

    # Media type colors for distribution charts
    MEDIA_COLORS = {
        'visual_novel': '#FF6B9D',  # Pink
        'manga': '#FFA500',         # Orange
        'anime': '#4FC3F7',         # Light Blue
        'book': '#8B4513',          # Brown
        'video': '#9C27B0',         # Purple
        'game': '#4CAF50',          # Green
        'manual': '#9E9E9E'         # Gray
    }
    
    def __init__(self):
        self.session = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def _read_capped(self, response) -> bytes:
        """Read a response body, refusing oversized payloads (header or actual size)."""
        declared = response.content_length
        if declared is not None and declared > MAX_CHART_BYTES:
            raise Exception(f"Chart response too large ({declared} bytes)")
        # Read the full body in chunks, aborting past the cap. (content.read(n)
        # returns only the first buffered chunk, which truncates the image.)
        content = b""
        async for chunk in response.content.iter_chunked(65536):
            content += chunk
            if len(content) > MAX_CHART_BYTES:
                raise Exception("Chart response exceeded size cap")
        return content
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    def _get_bar_chart_config(self, labels: List[str], values: List[float], 
                             goal_line: Optional[int] = None) -> Dict[str, Any]:
        """Generate bar chart configuration for QuickChart.io"""
        
        annotations = []
        if goal_line and goal_line > 0:
            annotations.append({
                "type": "line",
                "mode": "horizontal", 
                "value": goal_line,
                "scaleID": "y-axis-0",
                "borderColor": self.SECONDARY_COLOR,
                "borderWidth": 1,
                "borderDash": [5, 5]
            })
        
        return {
            "version": "2",
            "backgroundColor": self.BACKGROUND_COLOR,
            "width": 500,
            "height": 300,
            "devicePixelRatio": 1,
            "format": "png",
            "chart": {
                "type": "bar",
                "data": {
                    "labels": labels,
                    "datasets": [{
                        "data": values,
                        "backgroundColor": self.PRIMARY_COLOR
                    }]
                },
                "options": {
                    "annotation": {
                        "annotations": annotations
                    },
                    "legend": {
                        "display": False
                    },
                    "layout": {
                        "padding": {
                            "left": 10,
                            "right": 10,
                            "top": 30,
                            "bottom": 30
                        }
                    },
                    "scales": {
                        "xAxes": [{
                            "ticks": {
                                "fontColor": self.TEXT_COLOR
                            },
                            "gridLines": {
                                "display": False
                            }
                        }],
                        "yAxes": [{
                            "ticks": {
                                "fontColor": self.TEXT_COLOR
                            },
                            "gridLines": {
                                "color": self.GRID_COLOR,
                                "zeroLineColor": self.TEXT_COLOR
                            }
                        }]
                    }
                }
            }
        }
    
    async def generate_duration_chart(self, daily_data: Dict[str, float], 
                                    start_date: datetime, end_date: datetime,
                                    goal_minutes: Optional[int] = None) -> BytesIO:
        """
        Generate a daily duration chart
        
        Args:
            daily_data: Dict mapping date strings to minutes (float)
            start_date: Start date for the chart
            end_date: End date for the chart  
            goal_minutes: Optional daily goal to show as horizontal line
            
        Returns:
            BytesIO containing the chart image
        """
        if not self.session:
            raise RuntimeError("ChartGenerator must be used as async context manager")
        
        # Determine whether to group by month (for long periods)
        delta_days = (end_date - start_date).days
        use_monthly = delta_days > 90
        
        labels = []
        values = []
        
        if use_monthly:
            # Group by month
            monthly_data = {}
            current_date = start_date.replace(day=1)
            
            while current_date <= end_date:
                month_key = current_date.strftime("%Y-%m")
                monthly_data[month_key] = 0
                current_date = (current_date.replace(day=28) + timedelta(days=4)).replace(day=1)
            
            # Sum daily data into monthly buckets
            for date_str, minutes in daily_data.items():
                try:
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                    month_key = date_obj.strftime("%Y-%m")
                    if month_key in monthly_data:
                        monthly_data[month_key] += minutes  # minutes is already a float
                except ValueError:
                    continue
            
            # Create chart data
            for month_key in sorted(monthly_data.keys()):
                labels.append(month_key)
                values.append(round(monthly_data[month_key]))
                
            # Don't show daily goal line for monthly view
            goal_minutes = None
            
        else:
            # Daily view
            current_date = start_date
            while current_date <= end_date:
                date_str = current_date.strftime("%Y-%m-%d")
                labels.append(current_date.strftime("%m-%d"))
                values.append(round(daily_data.get(date_str, 0)))
                current_date += timedelta(days=1)
        
        # Generate chart
        chart_config = self._get_bar_chart_config(labels, values, goal_minutes)
        
        try:
            async with self.session.post(
                self.QUICKCHART_URL,
                json=chart_config,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status != 200:
                    raise Exception(f"Chart generation failed with status {response.status}")

                content = await self._read_capped(response)
                return BytesIO(content)

        except Exception as e:
            logger.error(f"Error generating chart: {e}")
            raise

    async def generate_line_chart(self, daily_data: Dict[str, float],
                                  start_date: datetime, end_date: datetime,
                                  metric: str = "duration") -> BytesIO:
        """
        Generate a modern line chart for timeline view

        Args:
            daily_data: Dict mapping date strings to metric values
            start_date: Start date for the chart
            end_date: End date for the chart
            metric: Metric being displayed ("duration", "characters", "pages", "episodes")

        Returns:
            BytesIO containing the chart image
        """
        if not self.session:
            raise RuntimeError("ChartGenerator must be used as async context manager")

        # Determine whether to group by month (for long periods)
        delta_days = (end_date - start_date).days
        use_monthly = delta_days > 90

        labels = []
        values = []

        if use_monthly:
            # Group by month
            monthly_data = {}
            current_date = start_date.replace(day=1)

            while current_date <= end_date:
                month_key = current_date.strftime("%Y-%m")
                monthly_data[month_key] = 0
                current_date = (current_date.replace(day=28) + timedelta(days=4)).replace(day=1)

            # Sum daily data into monthly buckets
            for date_str, value in daily_data.items():
                try:
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                    month_key = date_obj.strftime("%Y-%m")
                    if month_key in monthly_data:
                        monthly_data[month_key] += value
                except ValueError:
                    continue

            # Create chart data
            for month_key in sorted(monthly_data.keys()):
                labels.append(month_key)
                values.append(round(monthly_data[month_key], 2))

        else:
            # Daily view
            current_date = start_date
            while current_date <= end_date:
                date_str = current_date.strftime("%Y-%m-%d")
                labels.append(current_date.strftime("%m-%d"))
                values.append(round(daily_data.get(date_str, 0), 2))
                current_date += timedelta(days=1)

        # Determine y-axis label based on metric
        y_axis_labels = {
            "duration": "Hours",
            "characters": "Characters",
            "pages": "Pages",
            "episodes": "Episodes"
        }
        y_label = y_axis_labels.get(metric, "Value")

        # Build line chart config
        config = {
            "version": "2",
            "backgroundColor": self.BACKGROUND_COLOR,
            "width": 500,
            "height": 300,
            "devicePixelRatio": 1,
            "format": "png",
            "chart": {
                "type": "line",
                "data": {
                    "labels": labels,
                    "datasets": [{
                        "data": values,
                        "borderColor": self.PRIMARY_COLOR,
                        "backgroundColor": self.PRIMARY_COLOR + "40",  # 25% opacity
                        "fill": True,
                        "tension": 0.4,  # Smooth curves
                        "borderWidth": 2,
                        "pointRadius": 3,
                        "pointBackgroundColor": self.PRIMARY_COLOR,
                        "pointBorderColor": "#FFFFFF",
                        "pointBorderWidth": 1,
                        "pointHoverRadius": 5
                    }]
                },
                "options": {
                    "legend": {
                        "display": False
                    },
                    "layout": {
                        "padding": {
                            "left": 10,
                            "right": 10,
                            "top": 30,
                            "bottom": 30
                        }
                    },
                    "scales": {
                        "xAxes": [{
                            "ticks": {
                                "fontColor": self.TEXT_COLOR,
                                "maxRotation": 45,
                                "minRotation": 0
                            },
                            "gridLines": {
                                "display": False
                            }
                        }],
                        "yAxes": [{
                            "ticks": {
                                "fontColor": self.TEXT_COLOR,
                                "beginAtZero": True
                            },
                            "gridLines": {
                                "color": self.GRID_COLOR,
                                "zeroLineColor": self.TEXT_COLOR
                            },
                            "scaleLabel": {
                                "display": True,
                                "labelString": y_label,
                                "fontColor": self.TEXT_COLOR
                            }
                        }]
                    }
                }
            }
        }

        # Make request to QuickChart API
        try:
            async with self.session.post(
                self.QUICKCHART_URL,
                json=config,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status != 200:
                    raise Exception(f"Chart generation failed with status {response.status}")

                content = await self._read_capped(response)
                return BytesIO(content)

        except Exception as e:
            logger.error(f"Error generating line chart: {e}")
            raise

    async def generate_media_distribution_chart(self, media_breakdown: List[Dict[str, Any]],
                                               metric: str = "duration") -> BytesIO:
        """
        Generate pie chart for media type distribution

        Args:
            media_breakdown: List of dicts with media_type, count, duration, etc.
            metric: Which metric to visualize ("duration" or "count")

        Returns:
            BytesIO containing the chart image
        """
        if not self.session:
            raise RuntimeError("ChartGenerator must be used as async context manager")

        if not media_breakdown:
            raise ValueError("No media data to chart")

        # Extract labels and values
        labels = []
        values = []
        colors = []

        for item in media_breakdown:
            media_type = item.get('media_type', 'manual')

            # Format label
            label = media_type.replace('_', ' ').title()
            labels.append(label)

            # Get value based on metric
            if metric == "duration":
                # Convert seconds to hours for display
                value = (item.get('duration', 0) or 0) / 3600.0
            elif metric == "characters":
                value = item.get('characters', 0) or 0
            elif metric == "pages":
                value = item.get('pages', 0) or 0
            elif metric == "episodes":
                value = item.get('episodes', 0) or 0
            else:  # count
                value = item.get('count', 0) or 0

            values.append(round(value, 2))

            # Get color for this media type
            colors.append(self.MEDIA_COLORS.get(media_type, self.PRIMARY_COLOR))

        # Build pie chart config
        config = {
            "version": "2",
            "backgroundColor": self.BACKGROUND_COLOR,
            "width": 500,
            "height": 400,
            "devicePixelRatio": 1,
            "format": "png",
            "chart": {
                "type": "doughnut",
                "data": {
                    "labels": labels,
                    "datasets": [{
                        "data": values,
                        "backgroundColor": colors,
                        "borderWidth": 2,
                        "borderColor": self.BACKGROUND_COLOR
                    }]
                },
                "options": {
                    "legend": {
                        "display": True,
                        "position": "right",
                        "labels": {
                            "fontColor": self.TEXT_COLOR,
                            "fontSize": 12,
                            "padding": 15
                        }
                    },
                    "layout": {
                        "padding": {
                            "left": 20,
                            "right": 20,
                            "top": 20,
                            "bottom": 20
                        }
                    },
                    "plugins": {
                        "datalabels": {
                            "color": "#FFFFFF",
                            "font": {
                                "weight": "bold",
                                "size": 12
                            },
                            "formatter": "(value, context) => {const total = context.dataset.data.reduce((a, b) => a + b, 0); const percentage = ((value / total) * 100).toFixed(1); return percentage > 5 ? percentage + '%' : '';}"
                        }
                    }
                }
            }
        }

        # Make request to QuickChart API
        try:
            async with self.session.post(
                self.QUICKCHART_URL,
                json=config,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status != 200:
                    raise Exception(f"Chart generation failed with status {response.status}")

                content = await self._read_capped(response)
                return BytesIO(content)

        except Exception as e:
            logger.error(f"Error generating pie chart: {e}")
            raise

    def calculate_stats(self, daily_data: Dict[str, float]) -> Dict[str, Any]:
        """Calculate statistics from daily data"""
        if not daily_data:
            return {
                "total_minutes": 0,
                "average_minutes": 0,
                "highest_minutes": 0,
                "highest_day": "N/A",
                "days_with_activity": 0
            }
        
        values = list(daily_data.values())
        non_zero_values = [v for v in values if v > 0]
        
        total_minutes = sum(values)
        highest_minutes = max(values)
        highest_day = "N/A"
        
        # Find the day with highest activity
        for date_str, minutes in daily_data.items():
            if minutes == highest_minutes:
                try:
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                    highest_day = date_obj.strftime("%m-%d")
                    break
                except ValueError:
                    pass
        
        return {
            "total_minutes": round(total_minutes),
            "average_minutes": round(total_minutes / len(values)) if values else 0,
            "highest_minutes": round(highest_minutes),
            "highest_day": highest_day,
            "days_with_activity": len(non_zero_values)
        }


async def generate_activity_chart(activities: List[Dict[str, Any]], 
                                start_date: datetime, end_date: datetime,
                                goal_minutes: Optional[int] = None,
                                metric: str = "duration") -> Tuple[BytesIO, Dict[str, Any]]:
    """
    Generate activity chart from activity list
    
    Args:
        activities: List of activity dictionaries
        start_date: Start date for chart
        end_date: End date for chart
        goal_minutes: Optional daily goal (only applies to duration metric)
        metric: Metric to chart ("duration", "characters", "pages", or "episodes")
        
    Returns:
        Tuple of (chart_image, stats_dict)
    """
    # Group activities by date
    daily_data = {}
    
    for activity in activities:
        try:
            # Parse activity date
            if isinstance(activity['date'], str):
                activity_date = datetime.fromisoformat(activity['date'].replace('Z', '+00:00'))
            else:
                activity_date = activity['date']
            
            date_str = activity_date.strftime("%Y-%m-%d")
            
            # Initialize date if not present
            if date_str not in daily_data:
                daily_data[date_str] = 0
            
            # Extract value based on metric
            if metric == "duration":
                duration_seconds = activity.get('duration', 0)
                duration_minutes = duration_seconds / 60.0  # Convert seconds to minutes
                daily_data[date_str] += duration_minutes
            else:
                # For characters/pages, extract from meta
                meta = activity.get('meta', {})
                value = meta.get(metric, 0) if isinstance(meta.get(metric), (int, float)) else 0
                daily_data[date_str] += value
            
        except (ValueError, KeyError) as e:
            logger.warning(f"Skipping invalid activity: {e}")
            continue
    
    # Generate chart and calculate stats
    # Only pass goal for duration metric
    goal_value = goal_minutes if metric == "duration" else None
    
    async with ChartGenerator() as chart_gen:
        chart_image = await chart_gen.generate_duration_chart(
            daily_data, start_date, end_date, goal_value
        )
        stats = chart_gen.calculate_stats(daily_data)
    
    return chart_image, stats