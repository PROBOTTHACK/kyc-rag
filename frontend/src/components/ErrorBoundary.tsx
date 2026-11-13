import { Component, type ReactNode } from "react";

import { Disclaimer } from "./Disclaimer";

interface Props {
  children: ReactNode;
  /** When this value changes (e.g. the route), a shown error is cleared. */
  resetKey?: string;
  /** Render the disclaimer above the fallback: for the top-level boundary, which replaces
   * the whole shell (and with it the layout's disclaimer). */
  withDisclaimer?: boolean;
}

interface State {
  failed: boolean;
}

/**
 * Catches render errors below it and shows a fixed message. The error's own message and
 * stack are never rendered (they could contain data from an API response), and nothing is
 * reported anywhere: there is no telemetry (M5_PLAN §E rule 8).
 */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  override componentDidUpdate(prev: Props): void {
    if (this.state.failed && prev.resetKey !== this.props.resetKey) {
      this.setState({ failed: false });
    }
  }

  override render(): ReactNode {
    if (!this.state.failed) {
      return this.props.children;
    }
    return (
      <>
        {this.props.withDisclaimer === true && <Disclaimer />}
        <div role="alert" className="m-4 rounded border border-red-300 bg-red-50 p-4 text-red-950">
          <h2 className="text-lg font-semibold">This page could not be displayed.</h2>
          <p className="mt-1 text-sm">
            Something went wrong while showing this page. No result is shown instead.
          </p>
          <button
            type="button"
            className="mt-3 rounded border border-red-400 px-3 py-1 text-sm hover:bg-red-100"
            onClick={() => this.setState({ failed: false })}
          >
            Try again
          </button>
        </div>
      </>
    );
  }
}
