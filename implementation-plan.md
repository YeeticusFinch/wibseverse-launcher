# Account and SSO Integration Plan

1. Associate WibsVerse credentials with each Minecraft account; store passwords in the OS credential vault.
2. Sign in to WibsVerse and obtain a fresh single-use Minecraft link code.
3. Obtain the SSO mod's short-lived game credential and create its validated launcher handoff.
4. Preserve normal auto-join and automatically run `/linkaccount CODE` during cracked-account authentication.
5. Verify offline, linked, expired-code, account-switching, and Microsoft-account behavior.
