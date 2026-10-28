Feature: Layout gate

  Before a reviewer sees an artifact, the boot audit runs it at desktop, tablet
  and phone widths. A proven severe failure blocks the review and reaches the
  agent without anyone asking.

  Scenario Outline: A sound artifact opens straight into review
    Given <artifact>
    And the reviewer has the review session open
    Then the layout gate reports "clear"
    And the review is not blocked

    Examples:
      | artifact                                    |
      | a clean artifact                            |
      | a scaffolded artifact                       |
      | a scaffolded artifact whose cards hold code |
      | a scrolling-table artifact                  |

  Scenario: Saving a broken layout blocks the review and tells the agent
    Given a clean artifact
    And the reviewer has the review session open
    When the agent replaces the artifact with "broken.html"
    Then the review is blocked with at least 3 proven failures
    And "Show anyway" is offered
    And the agent receives a layout event
    And the layout event names the kinds:
      | escaped-markup |
      | clipped-text   |
      | h-overflow     |
    And the layout event carries overflow evidence
    When the reviewer chooses "Show anyway" on the curtain
    Then the review is not blocked
    When the agent replaces the artifact with "clean.html"
    Then the layout gate reports "clear"
    And the review is not blocked

  Scenario: A phone-only overflow is proven from a desktop review
    Given a viewport-overflow artifact
    And the reviewer opens a review session the layout gate blocks
    Then the audit proves a severe overflow on a phone
    And the audit finds no overflow on the desktop pass
    And the curtain shows the phone evidence
    And the artifact frame width is restored
    And the agent hears about the phone overflow

  Scenario: A blocked review stays blocked until the reviewer acts
    Given a viewport-overflow artifact
    And the reviewer opens a review session the layout gate blocks
    When the layout check time limit passes
    Then the curtain shows the phone evidence

  Scenario: Fixing a phone overflow clears the gate on the next save
    Given a viewport-overflow artifact
    And the reviewer opens a review session the layout gate blocks
    When the agent replaces the artifact with "clean.html"
    Then the layout gate reports "clear"
    And the review is not blocked
