Feature: Unrendered diagrams

  A Mermaid block that fails to render leaves its source showing as plain text.
  The review server reports it as a severe failure, so the review stays blocked
  until the agent fixes it or the reviewer chooses to look anyway.

  Background:
    Given a mermaid-broken artifact
    And the reviewer opens a review session the layout gate blocks

  Scenario: A diagram that never rendered reaches the agent on its own
    Then the "works" diagram rendered offline
    And the "broken" diagram still shows its Mermaid source
    And only the "broken" diagram is reported as unrendered
    And the unrendered finding is severe and names the diagram
    And the agent receives a layout event
    And the layout event names the kinds:
      | mermaid-render-failed |
