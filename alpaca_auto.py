"""Continuous, explicitly authorized Alpaca PAPER session manager."""
if __name__ == '__main__':
    import argparse
    from pathlib import Path
    from quant_lab.automation import run_alpaca
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute-paper', action='store_true', required=True)
    parser.parse_args()
    run_alpaca(Path(__file__).resolve().parent)
