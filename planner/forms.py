from dataclasses import fields

from django import forms

from ras_engine import BiofilterParams, Params

from .models import EquipmentStandard, Feed, Scenario


def _field_for(f, initial):
    meta = f.metadata
    label = meta["label"] + (f" [{meta['unit']}]" if meta.get("unit") else "")
    if isinstance(f.default, bool):
        return forms.TypedChoiceField(label=label, initial="True" if initial else "False",
                                      choices=[("True", "Yes"), ("False", "No")],
                                      coerce=lambda v: v == "True")
    if isinstance(f.default, str):
        return forms.CharField(label=label, initial=initial, required=False)
    if isinstance(f.default, int) and not isinstance(f.default, bool):
        return forms.IntegerField(label=label, initial=initial)
    return forms.FloatField(label=label, initial=initial,
                            widget=forms.NumberInput(attrs={"step": "any"}))


class ParamsForm(forms.Form):
    """Form generated from ras_engine.Params fields; keys look like 'growth.days'."""

    def __init__(self, *args, values: dict, groups=None, exclude=(), **kwargs):
        """groups: only these parameter groups (a workflow step shows its own); exclude: keys shown elsewhere."""
        super().__init__(*args, **kwargs)
        self.groups: dict[str, list[str]] = {}
        defaults = Params()
        for section in fields(defaults):
            for f in fields(getattr(defaults, section.name)):
                key = f"{section.name}.{f.name}"
                if (groups and f.metadata["group"] not in groups) or key in exclude:
                    continue
                self.fields[key] = _field_for(f, values.get(key, f.default))
                self.groups.setdefault(f.metadata["group"], []).append(key)

    def grouped(self):
        return [(g, [self[k] for k in keys]) for g, keys in self.groups.items()]


# Constants that can differ per RAS unit. Stocking count/weight and day count belong to the batch.
STAGE_EXCLUDE = {"growth.initial_count", "growth.initial_weight_g", "growth.days"}
BIO_EXCLUDE = {"design_weight_g", "temperature", "protein", "daily_feed_kg", "tank_volume_m3",
               "n_tanks", "n_ras_units", "retention_times"}


def stage_field_specs():
    """[(key, label, unit, group, default)] for the per-RAS constants panel."""
    specs = []
    defaults = Params()
    for section in fields(defaults):
        for f in fields(getattr(defaults, section.name)):
            key = f"{section.name}.{f.name}"
            if key not in STAGE_EXCLUDE:
                specs.append((key, f.metadata["label"], f.metadata.get("unit", ""),
                              f.metadata["group"], f.default))
    for f in fields(BiofilterParams):
        if f.name not in BIO_EXCLUDE:
            specs.append((f"bio.{f.name}", f.metadata["label"], f.metadata.get("unit", ""),
                          "Biofilter", f.default))
    return specs


def scenario_base_values(scenario) -> dict:
    """Scenario-level values (used wherever a RAS unit does not override them)."""
    from dataclasses import asdict
    base = scenario.engine_params().to_flat()
    base.update({f"bio.{k}": v for k, v in asdict(scenario.engine_biofilter()).items()})
    return base


def same_value(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return abs(float(a) - float(b)) <= 1e-12 * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


class BiofilterForm(forms.Form):
    def __init__(self, *args, values: dict, linked: dict | None = None, exclude=(), **kwargs):
        super().__init__(*args, **kwargs)
        for f in fields(BiofilterParams):
            if f.name in exclude:
                continue
            if linked and f.name in linked:  # comes from the simulation, read-only
                field = _field_for(f, linked[f.name])
                field.disabled = True
                field.label += " ← from simulation"
            else:
                field = _field_for(f, values.get(f.name, f.default))
            self.fields[f.name] = field


class ScenarioForm(forms.ModelForm):
    class Meta:
        model = Scenario
        fields = ["name", "temperature_steps", "use_feed_catalog"]


class StandardForm(forms.ModelForm):
    class Meta:
        model = EquipmentStandard
        fields = ["category", "name", "capacity", "unit_price", "note"]
        widgets = {"capacity": forms.NumberInput(attrs={"step": "any"}),
                   "unit_price": forms.NumberInput(attrs={"step": "any"})}

    def clean_capacity(self):
        value = self.cleaned_data["capacity"]
        if value <= 0:
            raise forms.ValidationError("Capacity must be above zero.")
        return value


class FeedForm(forms.ModelForm):
    class Meta:
        model = Feed
        fields = ["name", "min_weight_g", "max_weight_g", "protein_pct", "fcr"]
        widgets = {k: forms.NumberInput(attrs={"step": "any"})
                   for k in ["min_weight_g", "max_weight_g", "protein_pct", "fcr"]}

    def clean(self):
        data = super().clean()
        lo, hi = data.get("min_weight_g"), data.get("max_weight_g")
        if lo is not None and hi is not None and lo >= hi:
            raise forms.ValidationError("Max. weight must be greater than min. weight.")
        return data
