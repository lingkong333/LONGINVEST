from long_invest.entrypoints.monitor_scheduler import main, run

__all__ = ["main", "run"]


if __name__ == "__main__":
    main(service="longinvest-background")
