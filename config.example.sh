#!/usr/bin/env bash
# Copy to config.sh (gitignored) and fill in your real values, then:
#   source config.sh && python3 textwatcher.py

export OPENAI_API_KEY=""            # optional -- only used to classify unrecognized poll types
export IMESSAGE_RECIPIENT=""        # your phone number ("+15555555555") or iMessage email
export SSO_EMAIL=""                 # optional -- auto-fills PollEv's email step on login-required polls
