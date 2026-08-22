import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

describe("vitest harness", () => {
  it("renders DOM through testing-library with jest-dom matchers", () => {
    render(<main data-testid="harness">harness ready</main>);
    expect(screen.getByTestId("harness")).toBeInTheDocument();
    expect(screen.getByText("harness ready")).toBeVisible();
  });
});
