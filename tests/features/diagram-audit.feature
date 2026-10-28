Feature: Unrendered diagrams

  A Mermaid block that fails to render leaves its source showing as plain text.
  The page audit runs before the offline renderer, so a second pass is the only
  thing that can tell the agent the diagram is missing.

  Background:
    Given a mermaid-broken artifact
    And the reviewer opens the review session

  Scenario: A diagram that never rendered reaches the agent on its own
    Then the "works" diagram rendered offline
    And the "broken" diagram still shows its Mermaid source
    And only the "broken" diagram is reported as unrendered
    And the unrendered finding is severe and names the diagram
    And the agent receives a layout event
    And the layout event names the kinds:
      | mermaid-render-failed |
    And the review is blocked with at least 1 proven failures
