"""
Shared constants for Muramasa (VNCRbot)
"""

# NSFW detection threshold for sexual content.
# Scale: 0 = Safe, 1 = Suggestive, 2 = Explicit (continuous float values)
# 
# Note: Although the scale suggests "Explicit" is at 2.0, VNDB labels covers "Explicit" and
# shows its explicit content warning well below 2.0. This value matches that observed VNDB
# behavior rather than the nominal top of the scale.
# 
# ⚠️ IMPORTANT: Changing this threshold affects ALL NSFW detection across the bot.
# Lower values = more content flagged as NSFW (stricter filtering)
# Higher values = less content flagged as NSFW (more permissive)
NSFW_SEXUAL_THRESHOLD: float = 1.5
