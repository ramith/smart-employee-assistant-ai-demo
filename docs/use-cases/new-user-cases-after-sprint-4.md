I want to modify the bussiness usecases drastically.

user personas:
 HR Admins, Employee.

HR admin can do everything a employee can do.


new usercase : 1

    newly joined employee.

    HR admin (user = hr_admin_user) wants to allocate a seat/cubicle/office for next joinee. (e.g. 3rd floor, cubicle #23.) <-- HR agent will do this. what this really means is REST API will make a record saying new employee ( username, first name last name, email) is assigned cubicle #23 ( <--- there is a cubicles from 1 -- 100 in the building, some are marked occupied. REST api's data model will keep this in memory).


    Then HR admin could want to issue an laptop. <--- IT agent will do this. what this really means is REST API will keep track of that fact that employee (username, first name , last name , email) is issued an laptop.



new user case : 2
    New Employee logs in to the system. he can ask where is my assigned cubile is located. he can inquire after what laptop he is allocated ( with serial number, model).


new user case : 3
    An Employee can ask for a leave (check the legacy demo source for how this works). once requested, that lands on HR admin's GUI for an approval. HR admin can approve the request or let it be (for the time being).


new user case : 4
    Employee can check the status of applied leaves and it status.



new user case : 5

    HR admin can query for all the leave requests currently in approval stage. shown as a table in the GUI (user name, leave type, duration, start date)


new user case : 6
    HR admin can query for devices allocated for a perticular user (idenfied by either user name, firstname + last name).

    HR admin can generate a table in GUI of laptop assignments (user name , model)


   HR admin can generate a table in GUI of cubicle assignments ( user name , model)



when agent works each user can combine questions  scenarios from above as they whish.


from a security stand point, this means there need to be new oauth scope for IT related scenarios such as it_self_rest
other than that I don't see a requirement define new scopes. but i'm open to it.


REST APIS keep everything in memory given that this is a demo. if it goes down data is lost but thats ok.

