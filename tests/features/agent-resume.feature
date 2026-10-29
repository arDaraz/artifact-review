Feature: Resuming the agent from the review

  A review outlives the agent session that opened it. When no agent polls,
  the reviewer can reopen that session from the review instead of finding it
  in a terminal.

  Background:
    Given a clean artifact

  Scenario: Resume stays dimmed when no agent session is recorded
    Given the reviewer has the review session open
    Then the resume action is dimmed

  Scenario: Resume is available when the recorded agent is not polling
    Given the review server is running
    And the agent session that opened the review is recorded
    And the reviewer has the review session open
    Then the resume action is available

  Scenario: Resume dims while an agent polls
    Given the review server is running
    And the agent session that opened the review is recorded
    And the reviewer has the review session open
    When the agent reports that it is listening
    Then the resume action is dimmed

  Scenario: Resume refuses an agent session that is still open
    Given the review server is running
    And the agent session that opened the review is still running
    And the reviewer has the review session open
    When the reviewer chooses to resume the agent
    Then the reviewer is told the agent session is still open
