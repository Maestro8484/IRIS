"""
hardware/io.py - GPIO and physical input
Button on GPIO17 (active-low). Call setup_button() once at startup.
"""

import RPi.GPIO as GPIO
from core.config import BUTTON_PIN


def setup_button():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN)


def button_pressed() -> bool:
    return GPIO.input(BUTTON_PIN) == 0


def gpio_cleanup():
    GPIO.cleanup()
