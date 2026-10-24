class EngineInputError(ValueError):
    """Invalid user input — caught by existing ValueError handlers."""


from .biofilter import (LINKED_FIELDS, biofilter_row, biofilter_table, design_point,
                        link_to_simulation, max_retention_min)
from .equipment import CATEGORIES, size_item, size_tanks, stage_requirements
from .feed import Feed, select_feed
from .params import BiofilterParams, Params
from .production import PlanParams, RasStage, growth_curve, run_plan, stage_index
from .simulate import simulate, summary, weekly
