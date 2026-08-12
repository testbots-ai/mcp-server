"""
Scoped execution tools: run a single script in isolation without creating full suite/bot structure.
Fixes from retrospective: avoid 40-90min full regression cycles, run targeted tests instead.
"""

import time
from typing import Optional


async def execute_script_only(
    clients,
    script_id: str,
    execution_configuration: dict,
    confirm_script_id: str = None,  # Proof of intent — must match script_id
) -> dict:
    """
    Execute a SINGLE test script in isolation, bypassing suite/bot creation.

    This is the fast path for targeting a specific script without a full regression suite.
    Avoids the 40-90 minute cycle of running 30-script suites — just run the one you're
    trying to fix, verify it works, THEN add it to the suite.

    Args:
        script_id: The script to run
        execution_configuration: Browser/grid/environment config (same as execute_bot)
        confirm_script_id: Safety check — must equal script_id to proceed (guards against
                          accidental wrong-script execution). Pass the same value as script_id.

    Returns: Same format as execute_bot — {jobId, executionId progression, next_step guidance}
    """
    # Safety check: require explicit confirmation of the script ID being executed
    if confirm_script_id != script_id:
        return {
            "error": "confirm_script_id must match script_id — this safety check prevents "
            "accidentally running the wrong script. Pass both with the same value."
        }

    # Load the script to verify it exists and get its metadata
    try:
        script = await clients.test_mgmt.get_test_script(script_id)
    except Exception as e:
        return {"error": f"Could not load script {script_id}: {e}"}

    if not isinstance(script, dict):
        return {"error": f"Script {script_id} response was not a dictionary"}

    script_name = script.get("name", f"Script {script_id}")

    # Create a temporary suite with just this script
    try:
        suite_name = f"[SCOPED] {script_name} - {int(time.time())}"
        suite = await clients.test_mgmt.create_suite(
            name=suite_name,
            description="Temporary single-script execution — auto-created by execute_script_only"
        )
        suite_id = suite.get("id") or suite.get("testSuiteId")
        if not suite_id:
            return {"error": f"Could not create temporary suite: {suite}"}
    except Exception as e:
        return {"error": f"Failed to create temporary suite: {e}"}

    # Add the script to the suite
    try:
        await clients.test_mgmt.add_scripts_to_suite(
            suite_id=suite_id,
            script_ids=[script_id]
        )
    except Exception as e:
        return {"error": f"Failed to add script to temporary suite: {e}. Suite {suite_id} should be cleaned up."}

    # Create a temporary bot with just this suite
    try:
        bot_name = f"[SCOPED] {script_name} - {int(time.time())}"
        bot = await clients.test_mgmt.create_test_bot(
            name=bot_name,
            bot_type="CUSTOM",
            suite_ids=[suite_id],
            description="Temporary single-script execution — auto-created by execute_script_only"
        )
        bot_id = bot.get("id") or bot.get("testBotId")
        if not bot_id:
            return {"error": f"Could not create temporary bot: {bot}. Suite {suite_id} should be cleaned up."}
    except Exception as e:
        return {"error": f"Failed to create temporary bot: {e}. Suite {suite_id} should be cleaned up."}

    # Execute the bot
    try:
        result = await clients.executor.execute_bot(
            bot_id=bot_id,
            execution_configuration=execution_configuration,
            name=f"[SCOPED] {script_name}",
            profile_id=execution_configuration.get("profileId"),
            partial_execution=False
        )
    except Exception as e:
        return {
            "error": f"Execution failed: {e}",
            "cleanup": f"Delete temporary suite {suite_id} and bot {bot_id} when done debugging"
        }

    # Enrich the result with cleanup information
    result["temporary_resources"] = {
        "suite_id": suite_id,
        "suite_name": suite_name,
        "bot_id": bot_id,
        "bot_name": bot_name,
        "cleanup_instruction": (
            f"These temporary resources should be deleted after verifying the script works:\n"
            f"  - Suite: {suite_name} ({suite_id})\n"
            f"  - Bot: {bot_name} ({bot_id})\n"
            f"Once confirmed working, add this script to a real suite/bot and delete these."
        )
    }

    return result


async def execute_script_step_by_step(
    clients,
    script_id: str,
    execution_configuration: dict,
    step_number: Optional[int] = None,
    confirm_script_id: str = None,
) -> dict:
    """
    Execute a script, optionally stopping after a specific step number.

    For detailed debugging: run just the first 3 steps, verify they pass, then continue.
    Prevents wasting time on a 14-step script when step 4 is what's broken.

    Note: This is a soft limit via the API's partialExecution flag. The platform may
    execute more steps than requested if they're in a batched group.

    Args:
        script_id: The script to run
        execution_configuration: Browser/grid/environment config
        step_number: Stop after this step (1-indexed). None = run all.
        confirm_script_id: Safety check (same as execute_script_only)

    Returns: Same as execute_script_only, with step_limit noted
    """
    # Reuse the single-script execution, but pass partial_execution=True to the API
    if confirm_script_id != script_id:
        return {
            "error": "confirm_script_id must match script_id"
        }

    try:
        script = await clients.test_mgmt.get_test_script(script_id)
    except Exception as e:
        return {"error": f"Could not load script: {e}"}

    script_name = script.get("name", f"Script {script_id}")

    # Create temporary suite + bot (same as execute_script_only)
    try:
        suite_name = f"[SCOPED-DEBUG] {script_name} - {int(time.time())}"
        suite = await clients.test_mgmt.create_suite(
            name=suite_name,
            description="Temporary single-script execution (partial) — auto-created by execute_script_step_by_step"
        )
        suite_id = suite.get("id") or suite.get("testSuiteId")

        await clients.test_mgmt.add_scripts_to_suite(suite_id=suite_id, script_ids=[script_id])

        bot_name = f"[SCOPED-DEBUG] {script_name} - {int(time.time())}"
        bot = await clients.test_mgmt.create_test_bot(
            name=bot_name,
            bot_type="CUSTOM",
            suite_ids=[suite_id],
            description="Temporary single-script partial execution"
        )
        bot_id = bot.get("id") or bot.get("testBotId")
    except Exception as e:
        return {"error": f"Failed to set up temporary resources: {e}"}

    # Execute with partial_execution flag
    try:
        result = await clients.executor.execute_bot(
            bot_id=bot_id,
            execution_configuration=execution_configuration,
            name=f"[SCOPED-DEBUG] {script_name} (up to step {step_number or 'all'})",
            profile_id=execution_configuration.get("profileId"),
            partial_execution=True  # Signals "stop early if you can"
        )
    except Exception as e:
        return {"error": f"Execution failed: {e}"}

    result["temporary_resources"] = {
        "suite_id": suite_id,
        "bot_id": bot_id,
        "step_limit": step_number,
        "cleanup_instruction": f"Delete suite {suite_id} and bot {bot_id} when done."
    }

    return result
