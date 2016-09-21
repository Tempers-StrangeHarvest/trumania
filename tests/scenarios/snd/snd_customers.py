from __future__ import division
from datagenerator.core.circus import *
from datagenerator.core.actor import *
from datagenerator.core.util_functions import *
import pandas as pd


def create_customers(circus, params):

    logging.info(" adding customers")

    customers = Actor(size=params["n_customers"],
                      ids_gen=SequencialGenerator(prefix="CUST_"))

    logging.info(" adding 'possible sites' mobility relationship to customers")

    mobility_rel = customers.create_relationship(
        "POSSIBLE_SITES",
        seed=circus.seeder.next())

    mobility_df = pd.DataFrame.from_records(
        make_random_bipartite_data(
            customers.ids,
            circus.sites.ids,
            p=params["mean_known_sites_per_customer"]/params["n_sites"],
            seed=circus.seeder.next()),
        columns=["CID", "SID"])

    mobility_weight_gen = NumpyRandomGenerator(
        method="exponential", scale=1., seed=circus.seeder.next())

    mobility_rel.add_relations(
        from_ids=mobility_df["CID"],
        to_ids=mobility_df["SID"],
        weights=mobility_weight_gen.generate(mobility_df.shape[0]))

    logging.info(" assigning a first random site to each customer")
    customers.create_attribute(name="CURRENT_SITE",
                                    init_relationship="POSSIBLE_SITES")

    return customers


def add_mobility_action(circus, params):

    logging.info(" creating mobility action")

    mov_prof = [1., 1., 1., 1., 1., 1., 1., 1., 5., 10., 5., 1., 1., 1., 1.,
                1., 1., 5., 10., 5., 1., 1., 1., 1.]
    mobility_time_gen = CyclicTimerGenerator(
        clock=circus.clock,
        seed=circus.seeder.next(),
        config=CyclicTimerProfile(
            profile=mov_prof,
            profile_time_steps="1H",
            start_date=pd.Timestamp("12 September 2016 00:00.00"),
        )
    )

    gaussian_activity = NumpyRandomGenerator(
        method="normal", loc=params["mean_daily_mobility_activity"],
        scale=params["std_daily_mobility_activity"])
    mobility_activity_gen = TransformedGenerator(
        upstream_gen=gaussian_activity,
        f=lambda a: max(.5, a))

    mobility_action = circus.create_action(
        name="customer_mobility",

        initiating_actor=circus.customers,
        actorid_field="CUST_ID",

        timer_gen=mobility_time_gen,
        activity_gen=mobility_activity_gen
    )

    logging.info(" adding operations")

    mobility_action.set_operations(
        circus.customers.ops.lookup(
            actor_id_field="CUST_ID",
            select={"CURRENT_SITE": "PREV_SITE"}),

        # selects a destination site (or maybe the same as current... ^^)

        circus.customers \
            .get_relationship("POSSIBLE_SITES") \
            .ops.select_one(from_field="CUST_ID", named_as="NEW_SITE"),

        # update the SITE attribute of the customers accordingly
        circus.customers \
            .get_attribute("CURRENT_SITE") \
            .ops.update(
                actor_id_field="CUST_ID",
                copy_from_field="NEW_SITE"),

        circus.clock.ops.timestamp(named_as="TIME"),

        # create mobility logs
        operations.FieldLogger(log_id="customer_mobility_logs",
                               cols=["TIME", "CUST_ID", "PREV_SITE",
                                     "NEW_SITE"]),
    )


def add_purchase_sim_action(circus, params):

    purchase_timer_gen = DefaultDailyTimerGenerator(circus.clock,
                                                    circus.seeder.next())

    # between 1 to 6 SIM bought per year per customer
    purchase_activity_gen = NumpyRandomGenerator(
        method="choice", a=np.arange(1, 6) / 360.,
        seed=circus.seeder.next())

    purchase_action = circus.create_action(
        name="sim_purchases_to_pos",
        initiating_actor=circus.customers,
        actorid_field="CUST_ID",
        timer_gen=purchase_timer_gen,
        activity_gen=purchase_activity_gen)

    # Above a stock of 20, probability of re-stocking is close to 0
    # below it, it quickly rises to 5%
    # => chances getting out of stock < (1-.0.05)**20 ~= .35
    # => we can expect about 30% or so of POS to run out of stock regularly
    pos_bulk_purchase_trigger = DependentTriggerGenerator(
        value_to_proba_mapper=operations.logistic(k=-.5, x0=20, L=.05)
    )

    purchase_action.set_operations(

        circus.customers.ops.lookup(
            actor_id_field="CUST_ID",
            select={"CURRENT_SITE": "SITE"}),

        circus.sites.get_relationship("POS").ops.select_one(
            from_field="SITE",
            named_as="POS",

            weight=circus.pos.get_attribute_values("ATTRACTIVENESS"),

            # TODO: this means customer in a location without POS do not buy
            # anything => we could add a re-try mechanism here
            discard_empty=True),

        circus.pos.get_relationship("SIMS").ops.select_one(
            from_field="POS",
            named_as="SIM",

            pop=True,
            discard_empty=True),

        circus.sites.get_relationship("CELLS").ops.select_one(
            from_field="SITE",
            named_as="CELL",),

        SequencialGenerator(prefix="SIM_TX_")\
            .ops.generate(named_as="TX_ID"),

        ConstantGenerator(value=params["sim_price"])\
            .ops.generate(named_as="VALUE"),

        circus.clock.ops.timestamp(named_as="TIME"),

        FieldLogger(log_id="sim_purchases_to_pos"),


        circus.pos.get_relationship("SIMS").ops.get_neighbourhood_size(
            from_field="POS",
            named_as="CURRENT_POS_STOCK"
        ),

        pos_bulk_purchase_trigger.ops.generate(
            named_as="SHOULD_RESTOCK",
            observed_field="CURRENT_POS_STOCK"
        ),

        # => use pos_bulk_purchase_trigger and stock size (in relationship)
        circus.get_action("pos_to_dealer_sim_bulk_purchases").ops \
            .force_act_next(
            actor_id_field="POS",
            condition_field="SHOULD_RESTOCK"),
    )


